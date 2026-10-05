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
from ..sdtm_names import SUFFIXES, is_standard_variable

logger = get_logger()

PROPOSAL_FILE = "proposed_mappings.json"
# A real study has hundreds of fields; one request for all of them overruns the
# reply limit and comes back truncated. Small batches, a few at a time.
BATCH_FIELDS = 25
PARALLEL_CALLS = 4

_NAME = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")
# Test codes can be a single letter: potassium is K in CDISC Controlled Terminology.
_TESTCD = re.compile(r"^[A-Z][A-Z0-9_]{0,7}$")
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

DOMAIN_LABELS = {
    # the standard SDTMIG names, for domains no form rule produces
    "CO": "Comments", "SV": "Subject Visits", "SE": "Subject Elements", "EC": "Exposure as Collected",
    "PR": "Procedures", "QS": "Questionnaires", "FA": "Findings About", "TU": "Tumor Identification",
    "TR": "Tumor Results", "RS": "Disease Response", "DD": "Death Details", "MB": "Microbiology Specimen",
    "PC": "Pharmacokinetics Concentrations", "RP": "Reproductive System Findings", "CE": "Clinical Events",
    "HO": "Healthcare Encounters", "DV": "Protocol Deviations", "ML": "Meal Data", "IS": "Immunogenicity",
    **{domain: label for _, domain, label, _ in _FORM_RULES},
}

# field label → variable suffix (prefixed with the domain) or a full name
_FIELD_RULES: dict[str, Sequence[tuple[str, str]]] = {
    "DM": ((r"date of birth|birth ?date|\bdob\b", "BRTHDTC"), (r"\bsex\b|gender", "SEX"),
           (r"\brace\b", "RACE"), (r"ethnic", "ETHNIC"), (r"country", "COUNTRY"), (r"^age\b", "AGE")),
    # Specific before generic: "Action taken ... as a result of the Non-Serious
    # AE" is AEACN, not AESER, and is not an AE term.
    "AE": ((r"action taken", "AEACN"), (r"relat|causal", "AEREL"),
           (r"severity|intensity|grade", "AESEV"), (r"outcome", "AEOUT"),
           (r"(?<!non-)(?<!non )\bserious(ness)?\b", "AESER"),
           (r"term|event|verbatim|description", "AETERM")),
    "MH": ((r"term|condition|diagnosis|verbatim", "MHTERM"),),
    "CM": ((r"indication|reason for (medication|treatment|use)", "CMINDC"),
           (r"ongoing|continuing", "CMENRTPT"),
           (r"\bdose\b|dosage", "CMDOSE"), (r"unit", "CMDOSU"), (r"frequency", "CMDOSFRQ"),
           (r"route", "CMROUTE"),
           (r"^(medication|drug|treatment)( name)?$|(drug|medication|treatment|trade|generic) name", "CMTRT")),
    "EX": ((r"\bdose\b|amount", "EXDOSE"), (r"unit", "EXDOSU"), (r"route", "EXROUTE")),
    "DS": ((r"reason|status|disposition", "DSTERM"),),
}

# Collected on every form, submitted once in DM: the subject and site numbers.
_IDENTIFIERS: Sequence[tuple[str, str]] = (
    (r"^(subject|patient|participant) ?(identifier|id|number|no\.?)$|^screening (number|no\.?)$", "SUBJID"),
    (r"^(site|cent(er|re)|investigator site) ?(identifier|id|number|no\.?)?$", "SITEID"),
)

# findings tests: field label → TESTCD
_TESTS: Sequence[tuple[str, str]] = (
    (r"systolic", "SYSBP"), (r"diastolic", "DIABP"), (r"^(pulse|heart rate)\b", "PULSE"),
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


def _rule_mapping(form_oid: str, field_oid: str, label: str, domain: str, klass: str) -> Any:
    """A deterministic proposal for one field, or None."""
    if _is_question(label):
        # Label patterns are for terse EDC labels ("Event", "Start Date").
        # A full question ("What is the date the adverse event started?")
        # matches them by accident; the model reads it properly.
        return None
    date = _match(_DATES, label)
    if date and domain != "DM":
        return ProposedMapping(form_oid, field_oid, label, domain, f"{domain}{date}", reason="date pattern")
    if klass == "FINDINGS":
        # The vital-sign tests: "heart rate" inside an ECG rhythm label is not PULSE.
        testcd = _match(_TESTS, label) if domain == "VS" else ""
        if testcd:
            return ProposedMapping(form_oid, field_oid, label, domain, f"{domain}ORRES", testcd=testcd,
                                   reason=f"{testcd} result")
        return None
    variable = _match(_FIELD_RULES.get(domain, ()), label)
    if variable:
        return ProposedMapping(form_oid, field_oid, label, domain, variable, reason="CDISC label pattern")
    return None


def _domain_of_variable(variable: str) -> str:
    """`IEORRES` -> `IE`; '' when the name is not domain-prefixed (SEX, QVAL)."""
    if len(variable) > 2 and variable[2:] in SUFFIXES and _DOMAIN.match(variable[:2]):
        return variable[:2]
    return ""


_QUESTION_START = re.compile(
    r"^(what|which|when|where|why|how|who|is|are|was|were|did|does|do|has|have|had|can|will|if|"
    r"please|specify|indicate|provide|record|describe|list)\b", re.IGNORECASE)


def _is_question(label: str) -> bool:
    text = label.strip()
    return text.endswith("?") or bool(_QUESTION_START.match(text)) or len(text.split()) > 6


def _make_consistent(mappings: list[ProposedMapping]) -> int:
    """The same question on two forms gets the same annotation.

    A 12-lead and a telemetry ECG form both ask "Atrial fibrillation"; batches
    answered separately can code it ATFIB on one and ATRFIB on the other, which
    no reviewer accepts. The first rule-based (else most confident) answer wins.
    """
    chosen: dict[tuple[str, str], ProposedMapping] = {}
    ranked = sorted(mappings, key=lambda m: (m.source != "rule", -m.confidence))
    for mapping in ranked:
        chosen.setdefault((mapping.domain, normalise_label(mapping.field_label)), mapping)
    changed = 0
    for mapping in mappings:
        best = chosen[(mapping.domain, normalise_label(mapping.field_label))]
        if (mapping.variable, mapping.testcd, mapping.qnam) != (best.variable, best.testcd, best.qnam):
            mapping.variable, mapping.testcd, mapping.qnam = best.variable, best.testcd, best.qnam
            mapping.reason = f"{mapping.reason}; aligned with the same field on {best.form_oid}".lstrip("; ")
            changed += 1
    return changed


def normalise_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()


_SYSTEM = (
    "You map clinical-trial EDC fields to CDISC SDTM (SDTMIG 3.4) for an annotated CRF that a "
    "regulator will read. For each field, give the SDTM domain and variable it is collected into. "
    "Forms come from many sponsors and EDC systems, so judge each field by what it collects, not by "
    "the exact wording. A single form can hold sections that belong to different domains (a "
    "screening form with medical history, labs, physical exam and eligibility criteria); each "
    "field's section is given when the EDC export has one, and its domain follows what it "
    "collects, not the form.\n\n"
    "Conventions to follow:\n"
    "- Findings: a test result, measurement, or a yes/no 'is this abnormality present' question on a "
    "findings form (ECG, labs, vital signs, physical exam, questionnaires, eligibility criteria) is "
    "kind 'finding': variable --ORRES with the test code in testcd (e.g. VSORRES, testcd SYSBP). "
    "Each ECG finding, including every type of myocardial infarction, is a finding of its own, "
    "never 'skip'. Use the CDISC Controlled Terminology test code when one exists; otherwise make a "
    "clear code of at most 8 characters. Give the same question the same code on every form.\n"
    "- Questions about a test result belong to that test's record, in its domain, with the same "
    "testcd as the result (kind 'finding'): whether it is within the normal range is --NRIND, "
    "whether it is clinically significant is --CLSIG, its unit is --ORRESU, 'not done' is "
    "--STAT. These are standard variables, not supplemental qualifiers.\n"
    "- Eligibility: each inclusion or exclusion criterion question is IE, IEORRES, with the "
    "criterion identifier as testcd (INCL01, EXCL03, ...). Every criterion on the form is mapped, "
    "not only some.\n"
    "- Dates: the visit or assessment date on a form is that form's own --DTC (VSDTC, EGDTC, "
    "DSDTC, ...). A field may belong to a different domain from its form: the date of "
    "randomisation is DS.DSSTDTC (a disposition event), wherever it is collected. Set a field's "
    "domain only when it differs from its form's; otherwise leave it empty.\n"
    "- Pre-specified questions: a yes/no question about one named medication, class, event or "
    "procedure listed on the form ('Were any diuretics taken?', 'Has the subject had a "
    "colonoscopy?') records whether it occurred: --OCCUR in that domain (CMOCCUR, MHOCCUR, "
    "PROCCUR), and a reason it was not given is --REASOC or --REASND. A gate question that only "
    "opens a free-text list below it ('Were any medications taken?', 'Did the subject experience "
    "any adverse events?') is kind 'skip'; on a serious-adverse-event form that gate is AESER.\n"
    "- Adverse events: the seriousness criteria are their own variables - AESCONG, AESDISAB, "
    "AESDTH, AESHOSP, AESLIFE, AESMIE (other medically important), AESOD (overdose); toxicity "
    "grade is AETOXGR, ongoing is AEENRTPT or AEENRF.\n"
    "- Death: the details of a death (cause, location, witnessed, autopsy) are findings in DD "
    "(DDORRES with a DDTESTCD such as PRCDTH, LOCDTH); the date of death is DM.DTHDTC.\n"
    "- Supplemental qualifiers: collected data with no standard variable is kind 'supp', with a "
    "QNAM of at most 8 characters.\n"
    "- Not submitted: kind 'skip' only for operational fields, such as 'Was the form completed?', "
    "page status, a CRF number, or the gate questions above. Collected clinical data is never "
    "skipped; if you are unsure, map it and give a low confidence so a reviewer looks at it.\n"
    "- Names: use only variable names that SDTMIG defines for the domain (CMDOSE, CMENRTPT - never "
    "an invented one such as CMDOS or CMONGO), and only standard SDTM domains. Confidence is 0 to 1."
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
                                "domain": {"type": "string"},
                                "kind": {"type": "string", "enum": ["variable", "finding", "supp", "skip"]},
                                "variable": {"type": "string"},
                                "testcd": {"type": "string"},
                                "qnam": {"type": "string"},
                                "confidence": {"type": "number"},
                                "reason": {"type": "string"},
                            },
                            "required": ["field_oid", "domain", "kind", "variable", "testcd", "qnam", "confidence", "reason"],
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


def _ask_model(client: Any, forms: list[dict[str, Any]], note: str = "") -> list[dict[str, Any]]:
    """Structured calls for every form still needing proposals, in batches."""
    if not forms or client is None or not client.available():
        return []

    def ask(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        user = (
            "Propose SDTM mappings for these EDC forms and fields (labels as printed on the CRF):\n"
            + json.dumps(batch, indent=1)
            + (f"\n\n{note}" if note else "")
        )
        answer = client.json_call(system=_SYSTEM, user=user, schema=_SCHEMA, cache_salt="propose_specs/6")
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
        # The subject and site numbers go to DM whatever the form is called.
        identifier = _match(_IDENTIFIERS, item.label.strip())
        if identifier:
            proposal.mappings.append(ProposedMapping(item.form_oid, item.oid, item.label, "DM", identifier,
                                                     reason="subject/site identifier"))
            continue
        mapping = _rule_mapping(item.form_oid, item.oid, item.label, domain[0], domain[2]) if domain else None
        if mapping:
            proposal.mappings.append(mapping)
        else:
            entry = {"field_oid": item.oid, "label": item.label}
            if item.section:
                entry["section"] = item.section
            pending.setdefault(item.form_oid, []).append(entry)

    request = [
        {"form_oid": oid, "form_label": labels.get(oid, oid),
         "known_domain": proposal.domains.get(oid, ("",))[0], "fields": fields}
        for oid, fields in pending.items()
    ]
    answered: set[str] = set()
    field_labels = {(f.form_oid, f.oid): f.label for f in design.fields}

    def take(answers: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
        """Use the model's answers; return (form, field, why) for invalid ones."""
        invalid: list[tuple[str, str, str]] = []
        for form in answers:
            oid = str(form.get("form_oid") or "")
            domain = str(form.get("domain") or "").upper()
            if oid not in labels or not _DOMAIN.match(domain):
                continue
            proposal.domains.setdefault(oid, (domain, str(form.get("domain_label") or domain), str(form.get("class") or "")))
            form_domain = proposal.domains[oid][0]
            for entry in form.get("fields") or ():
                # a field may be submitted in another domain (randomisation date -> DS)
                own = str(entry.get("domain") or "").upper()
                domain = own if _DOMAIN.match(own) else form_domain
                # SDTM names carry their domain (IEORRES is IE): a variable never
                # goes into another domain because of the form it sits on.
                named = _domain_of_variable(str(entry.get("variable") or "").upper())
                if named and entry.get("kind") in ("variable", "finding"):
                    domain = named
                field_oid = str(entry.get("field_oid") or "")
                if (oid, field_oid) not in field_labels or f"{oid}.{field_oid}" in answered:
                    continue  # unknown, or already answered (a model may repeat itself)
                kind = entry.get("kind")
                if kind == "skip":
                    answered.add(f"{oid}.{field_oid}")
                    continue
                variable = str(entry.get("variable") or "").upper()
                qnam = str(entry.get("qnam") or "").upper()
                testcd = str(entry.get("testcd") or "").upper()
                if kind == "supp":
                    if not _NAME.match(qnam):
                        invalid.append((oid, field_oid, f"QNAM {qnam!r} is not a valid name"))
                        continue
                    variable = "QVAL"
                elif not _NAME.match(variable) or (kind == "finding" and not _TESTCD.match(testcd)):
                    invalid.append((oid, field_oid, f"{variable!r} / testcd {testcd!r} is not a valid name"))
                    continue
                elif not is_standard_variable(domain, variable):
                    # a plausible-looking name SDTM does not have (CMONGO, CMDOS)
                    invalid.append((oid, field_oid, f"{domain}.{variable} is not an SDTM variable"))
                    continue
                answered.add(f"{oid}.{field_oid}")
                proposal.mappings.append(ProposedMapping(
                    oid, field_oid, field_labels[(oid, field_oid)], domain, variable,
                    testcd=testcd if kind == "finding" else "", qnam=qnam if kind == "supp" else "",
                    confidence=float(entry.get("confidence") or 0), reason=str(entry.get("reason") or ""),
                    source="ai",
                ))
        return invalid

    invalid = take(_ask_model(client, request))
    if invalid:
        # One repair round: the same fields again, told what was wrong.
        retry = {(oid, field) for oid, field, _ in invalid}
        repair = [{**form, "fields": [f for f in form["fields"] if (form["form_oid"], f["field_oid"]) in retry]}
                  for form in request]
        repair = [form for form in repair if form["fields"]]
        problems = "; ".join(f"{field}: {why}" for _, field, why in invalid[:40])
        note = ("Earlier answers for these fields were rejected - " + problems + ". Use only variable "
                "names that SDTMIG defines for the domain, and a valid QNAM for supplemental qualifiers.")
        logger.info("Asking again for %d field(s) whose proposals were not valid SDTM", len(retry))
        for oid, field, why in take(_ask_model(client, repair, note)):
            logger.warning("Ignored a proposal for %s.%s: %s", oid, field, why)

    # A field the model left out of one batch is often answered for the same
    # question on another form (a 12-lead and a telemetry ECG form): reuse it.
    known = {}
    for mapping in proposal.mappings:
        known.setdefault((proposal.domains.get(mapping.form_oid, ("",))[0], normalise_label(mapping.field_label)),
                         mapping)
    borrowed = 0
    for oid, fields in pending.items():
        for item in fields:
            if f"{oid}.{item['field_oid']}" in answered:
                continue
            twin = known.get((proposal.domains.get(oid, ("",))[0], normalise_label(item["label"])))
            if twin and twin.form_oid != oid:
                proposal.mappings.append(ProposedMapping(
                    oid, item["field_oid"], item["label"], twin.domain, twin.variable, testcd=twin.testcd,
                    qnam=twin.qnam, confidence=twin.confidence, source=twin.source,
                    reason=f"same field as on {twin.form_oid}: {twin.reason}"))
                answered.add(f"{oid}.{item['field_oid']}")
                borrowed += 1
    if borrowed:
        logger.info("Reused %d mapping(s) from the same field on another form", borrowed)
    aligned = _make_consistent(proposal.mappings)
    if aligned:
        logger.info("Aligned %d mapping(s) with the same field on another form", aligned)
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
        domain, label, klass = proposal.domains.get(mapping.form_oid, (mapping.domain, "", ""))
        if mapping.domain and mapping.domain != domain:
            # collected here, submitted elsewhere (the subject number, into DM)
            domain, label, klass = mapping.domain, "", ""
        label = label or DOMAIN_LABELS.get(domain, domain)
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
