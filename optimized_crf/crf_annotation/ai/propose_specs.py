"""Propose SDTM mapping specs from the study design when none were supplied.

Someone trying the tool usually has a blank CRF and an EDC export but no
mapping specs in this package's JSON format. This module drafts them so a
first run can still happen — and is honest that it did:

* **Rules first.** Common CDISC patterns (a Demographics form is DM, "Date of
  Birth" is BRTHDTC, "Systolic Blood Pressure" is VSORRES where VSTESTCD =
  SYSBP) are matched deterministically. They cost nothing and never vary.
* **Then Claude, for the rest.** Fields no rule covers are sent — form and
  field *labels only*, never patient data — to the model, which answers with a
  domain, a variable, a confidence and a reason. Responses are cached like
  every other model call here, so a rerun proposes the same specs for free.
* **Always reviewed.** Every proposed mapping is recorded with where it came
  from. The output is ordinary spec JSON, so the rest of the pipeline is
  unchanged, and the run reports how many annotations rest on proposals.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..design import StudyDesign
from ..logging_utils import get_logger

logger = get_logger()

PROPOSAL_FILE = "proposed_mappings.json"
# A real study has hundreds of fields; one request for all of them overruns the
# reply limit and comes back truncated. Small batches, a few at a time.
BATCH_FIELDS = 25
PARALLEL_CALLS = 4

_NAME = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")
_DOMAIN = re.compile(r"^[A-Z]{2}$")

# form label → (domain, label, class)
_FORM_RULES: Sequence[tuple[str, str, str, str]] = (
    (r"demograph", "DM", "Demographics", "SPECIAL PURPOSE"),
    (r"adverse|\bae\b", "AE", "Adverse Events", "EVENTS"),
    (r"vital", "VS", "Vital Signs", "FINDINGS"),
    (r"con(comitant)?\.? ?med|prior.*med|medication", "CM", "Concomitant/Prior Medications", "INTERVENTIONS"),
    (r"medical history", "MH", "Medical History", "EVENTS"),
    (r"\blab|chemistry|hematology|haematology|urinalysis", "LB", "Laboratory Test Results", "FINDINGS"),
    (r"\becg\b|electrocardiogram", "EG", "ECG Test Results", "FINDINGS"),
    (r"physical exam", "PE", "Physical Examination", "FINDINGS"),
    (r"exposure|dosing|study drug|administration", "EX", "Exposure", "INTERVENTIONS"),
    (r"disposition|end of study|completion|discontinu", "DS", "Disposition", "EVENTS"),
    (r"eligib|inclusion|exclusion", "IE", "Inclusion/Exclusion Criteria Not Met", "FINDINGS"),
    (r"substance|tobacco|alcohol", "SU", "Substance Use", "INTERVENTIONS"),
    (r"subject characteristic", "SC", "Subject Characteristics", "FINDINGS"),
)

# field label → variable suffix (prefixed with the domain) or a full name
_FIELD_RULES: dict[str, Sequence[tuple[str, str]]] = {
    "DM": ((r"date of birth|birth ?date|\bdob\b", "BRTHDTC"), (r"\bsex\b|gender", "SEX"),
           (r"\brace\b", "RACE"), (r"ethnic", "ETHNIC"), (r"country", "COUNTRY"), (r"^age\b", "AGE")),
    "AE": ((r"term|event|verbatim|description", "AETERM"), (r"serious", "AESER"),
           (r"severity|intensity|grade", "AESEV"), (r"outcome", "AEOUT"),
           (r"relat|causal", "AEREL"), (r"action taken", "AEACN")),
    "MH": ((r"term|condition|diagnosis|verbatim", "MHTERM"),),
    "CM": ((r"medication|drug|treatment name|trade name|generic name", "CMTRT"), (r"indication", "CMINDC"),
           (r"\bdose\b|dosage", "CMDOSE"), (r"unit", "CMDOSU"), (r"frequency", "CMDOSFRQ"),
           (r"route", "CMROUTE")),
    "EX": ((r"\bdose\b|amount", "EXDOSE"), (r"unit", "EXDOSU"), (r"route", "EXROUTE")),
    "DS": ((r"reason|status|disposition", "DSTERM"),),
}

# findings tests: field label → TESTCD
_TESTS: Sequence[tuple[str, str]] = (
    (r"systolic", "SYSBP"), (r"diastolic", "DIABP"), (r"pulse|heart rate", "PULSE"),
    (r"temperature", "TEMP"), (r"respirat", "RESP"), (r"weight", "WEIGHT"),
    (r"height", "HEIGHT"), (r"\bbmi\b|body mass", "BMI"), (r"oxygen|spo2|saturation", "OXYSAT"),
)

_DATES: Sequence[tuple[str, str]] = (
    (r"start date|onset date|date started|from date", "STDTC"),
    (r"end date|stop date|resolution date|date stopped|to date", "ENDTC"),
    (r"date of (assessment|collection|measurement|exam)|assessment date|collection date|visit date", "DTC"),
)


@dataclass
class ProposedMapping:
    form_oid: str
    field_oid: str
    field_label: str
    domain: str
    variable: str
    """SDTM variable; for a findings test, the result variable (VSORRES)."""
    testcd: str = ""
    """Findings only: the test code the result is annotated under."""
    qnam: str = ""
    """Supplemental qualifiers only: the QNAM in SUPP--."""
    confidence: float = 1.0
    reason: str = ""
    source: str = "rule"
    """`rule` (deterministic CDISC pattern) or `ai` (model proposal)."""


@dataclass
class Proposal:
    mappings: list[ProposedMapping] = field(default_factory=list)
    domains: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    """form OID → (domain, domain label, class)."""
    unmapped: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "proposed": len(self.mappings),
            "by_rule": sum(m.source == "rule" for m in self.mappings),
            "by_ai": sum(m.source == "ai" for m in self.mappings),
            "unmapped_fields": len(self.unmapped),
        }


def _match(rules: Sequence[tuple[str, str]], text: str) -> str:
    for pattern, value in rules:
        if re.search(pattern, text, re.IGNORECASE):
            return value
    return ""


def _form_domain(label: str) -> Optional[tuple[str, str, str]]:
    for pattern, domain, domain_label, klass in _FORM_RULES:
        if re.search(pattern, label, re.IGNORECASE):
            return domain, domain_label, klass
    return None


def _rule_mapping(form_oid: str, field_oid: str, label: str, domain: str, klass: str) -> Optional[ProposedMapping]:
    """A deterministic proposal for one field, or None."""
    date = _match(_DATES, label)
    if date and domain != "DM":
        return ProposedMapping(form_oid, field_oid, label, domain, f"{domain}{date}", reason="date pattern")
    if klass == "FINDINGS":
        testcd = _match(_TESTS, label)
        if testcd:
            return ProposedMapping(form_oid, field_oid, label, domain, f"{domain}ORRES", testcd=testcd,
                                   reason=f"{testcd} result")
        return None
    variable = _match(_FIELD_RULES.get(domain, ()), label)
    if variable:
        return ProposedMapping(form_oid, field_oid, label, domain, variable, reason="CDISC label pattern")
    return None


_SYSTEM = (
    "You map clinical-trial EDC fields to CDISC SDTM (SDTMIG 3.4) for an annotated CRF. "
    "For each field, give the SDTM domain and variable it is collected into. Use findings "
    "structure for test results: variable --ORRES with the test code in testcd (e.g. VSORRES, "
    "testcd SYSBP). Use a supplemental qualifier (kind 'supp', with a QNAM of at most 8 "
    "characters) for collected data with no standard variable. Use kind 'skip' for operational "
    "fields that are not submitted (e.g. 'Was the form completed?', page status). Never invent a "
    "domain; only use standard SDTM domains. Confidence is 0 to 1."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "forms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "form_oid": {"type": "string"},
                    "domain": {"type": "string"},
                    "domain_label": {"type": "string"},
                    "class": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field_oid": {"type": "string"},
                                "kind": {"type": "string", "enum": ["variable", "finding", "supp", "skip"]},
                                "variable": {"type": "string"},
                                "testcd": {"type": "string"},
                                "qnam": {"type": "string"},
                                "confidence": {"type": "number"},
                                "reason": {"type": "string"},
                            },
                            "required": ["field_oid", "kind", "variable", "testcd", "qnam", "confidence", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["form_oid", "domain", "domain_label", "class", "fields"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["forms"],
    "additionalProperties": False,
}


def _batches(forms: list[dict[str, Any]], size: int = BATCH_FIELDS) -> list[list[dict[str, Any]]]:
    """Whole forms packed up to `size` fields per request; a bigger form is split."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    count = 0
    for form in forms:
        fields = form["fields"]
        for start in range(0, len(fields), size):
            part = {**form, "fields": fields[start:start + size]}
            if current and count + len(part["fields"]) > size:
                batches.append(current)
                current, count = [], 0
            current.append(part)
            count += len(part["fields"])
    if current:
        batches.append(current)
    return batches


def _ask_model(client: Any, forms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Structured calls for every form still needing proposals, in batches."""
    if not forms or client is None or not client.available():
        return []

    def ask(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        user = (
            "Propose SDTM mappings for these EDC forms and fields (labels as printed on the CRF):\n"
            + json.dumps(batch, indent=1)
        )
        answer = client.json_call(system=_SYSTEM, user=user, schema=_SCHEMA, cache_salt="propose_specs/2")
        return list((answer or {}).get("forms") or [])

    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        return [form for answered in pool.map(ask, _batches(forms)) for form in answered]


def propose_mappings(design: StudyDesign, client: Any = None) -> Proposal:
    """Rules first, then the model for whatever the rules couldn't place."""
    proposal = Proposal()
    labels = {form.oid: form.label for form in design.forms}
    pending: dict[str, list[dict[str, str]]] = {}

    for form in design.forms:
        found = _form_domain(form.label)
        if found:
            proposal.domains[form.oid] = found
    for item in design.fields:
        domain = proposal.domains.get(item.form_oid)
        mapping = _rule_mapping(item.form_oid, item.oid, item.label, domain[0], domain[2]) if domain else None
        if mapping:
            proposal.mappings.append(mapping)
        else:
            pending.setdefault(item.form_oid, []).append({"field_oid": item.oid, "label": item.label})

    request = [
        {"form_oid": oid, "form_label": labels.get(oid, oid),
         "known_domain": proposal.domains.get(oid, ("",))[0], "fields": fields}
        for oid, fields in pending.items()
    ]
    answered: set[str] = set()
    field_labels = {(f.form_oid, f.oid): f.label for f in design.fields}
    for form in _ask_model(client, request):
        oid = str(form.get("form_oid") or "")
        domain = str(form.get("domain") or "").upper()
        if oid not in labels or not _DOMAIN.match(domain):
            continue
        proposal.domains.setdefault(oid, (domain, str(form.get("domain_label") or domain), str(form.get("class") or "")))
        domain = proposal.domains[oid][0]
        for entry in form.get("fields") or ():
            field_oid = str(entry.get("field_oid") or "")
            if (oid, field_oid) not in field_labels:
                continue
            kind = entry.get("kind")
            answered.add(f"{oid}.{field_oid}")
            if kind == "skip":
                continue
            variable = str(entry.get("variable") or "").upper()
            qnam = str(entry.get("qnam") or "").upper()
            testcd = str(entry.get("testcd") or "").upper()
            if kind == "supp":
                if not _NAME.match(qnam):
                    continue
                variable = "QVAL"
            elif not _NAME.match(variable) or (kind == "finding" and not _NAME.match(testcd)):
                continue
            proposal.mappings.append(ProposedMapping(
                oid, field_oid, field_labels[(oid, field_oid)], domain, variable,
                testcd=testcd if kind == "finding" else "", qnam=qnam if kind == "supp" else "",
                confidence=float(entry.get("confidence") or 0), reason=str(entry.get("reason") or ""), source="ai",
            ))

    proposal.unmapped = [f"{oid}.{f['field_oid']}" for oid, fields in pending.items() for f in fields
                         if f"{oid}.{f['field_oid']}" not in answered]
    logger.info("Proposed %d mapping(s): %d by rule, %d by model; %d field(s) left unmapped",
                *proposal.counts.values())
    return proposal


def _copy(field_oid: str) -> list[dict[str, Any]]:
    return [{"function": "g_copy", "parameters": [{"name": "invar", "value": [field_oid]}]}]


def to_spec_documents(proposal: Proposal) -> dict[str, dict[str, Any]]:
    """The proposal as spec JSON documents, one per (source form, target domain)."""
    documents: dict[str, dict[str, Any]] = {}
    for mapping in proposal.mappings:
        domain, label, klass = proposal.domains.get(mapping.form_oid, (mapping.domain, mapping.domain, ""))
        if mapping.qnam:
            name = f"SUPP{domain}"
            doc = documents.setdefault(f"{mapping.form_oid}__{name}", {
                "domain": {"dataset": name, "label": f"Supplemental Qualifiers for {domain}", "class": "RELATIONSHIP",
                           "functions": [{"source_dataset": mapping.form_oid}]},
                "variables": [], "value_level": [],
            })
            doc["value_level"].append({"variable": "QVAL", "where_clause": f"QNAM EQ {mapping.qnam}",
                                       "functions": _copy(mapping.field_oid)})
            continue
        doc = documents.setdefault(f"{mapping.form_oid}__{domain}", {
            "domain": {"dataset": domain, "label": label, "class": klass or "",
                       "functions": [{"source_dataset": mapping.form_oid}]},
            "variables": [], "value_level": [],
        })
        if mapping.testcd:
            doc["value_level"].append({"dataset": domain, "variable": mapping.variable,
                                       "where_clause": f"{domain}TESTCD EQ {mapping.testcd}",
                                       "functions": _copy(mapping.field_oid)})
        else:
            doc["variables"].append({"dataset": domain, "variable": mapping.variable,
                                     "functions": _copy(mapping.field_oid)})
    return documents


def write_proposal(proposal: Proposal, directory: Path) -> tuple[Path, ...]:
    """Write the spec JSONs (and a reviewable summary) into `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, document in to_spec_documents(proposal).items():
        path = directory / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.json"
        path.write_text(json.dumps(document, indent=1))
        paths.append(path)
    (directory / PROPOSAL_FILE).write_text(json.dumps({
        "counts": proposal.counts,
        "mappings": [asdict(m) for m in proposal.mappings],
        "unmapped": proposal.unmapped,
    }, indent=1))
    return tuple(paths)
