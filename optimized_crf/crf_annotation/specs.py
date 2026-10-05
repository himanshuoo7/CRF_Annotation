"""Mapping specifications: raw CRF variable -> SDTM annotation text.

Each spec JSON is read once and turned into rows of
`(RSRC_DS, RSRC_VAR, TARGET_DS, ATEXT)`. The old MapProcessor built a
17-column frame of which only three columns were ever read again, and logged
six lines per variable - on a real study that is hundreds of thousands of log
lines and dominates the runtime.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import pandas as pd

from .constants import is_derived_variable
from .logging_utils import get_logger

logger = get_logger()

MAPPING_COLUMNS = ["RSRC_DS", "RSRC_VAR", "TARGET_DS", "TGT_VAR", "ATEXT"]
SUPP_PREFIX = "SUPP"
_SUPP_LABEL_MARKER = "Supplemental Qualifiers for"


def strip_supp(dataset: str) -> str:
    """`SUPPAE` -> `AE`; anything else is returned unchanged."""
    text = str(dataset or "")
    return text[len(SUPP_PREFIX):] if text.startswith(SUPP_PREFIX) else text


_IN_SUPP = re.compile(r"\bin (SUPP[A-Z0-9]{2,4})\s*$")


def domain_of(annotation: str) -> str:
    """Target domain encoded in an annotation: `AE.AETERM`, `AEFREQ in SUPPAE`."""
    text = str(annotation or "")
    supp = _IN_SUPP.search(text)
    if supp:
        return strip_supp(supp.group(1)).strip()
    if "." not in text:
        return ""
    return strip_supp(text.split(".", 1)[0]).strip()


@dataclass
class SpecCatalog:
    """Everything the spec files tell us, indexed for O(1) lookup."""

    mappings: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=MAPPING_COLUMNS))
    domain_labels: dict[str, str] = field(default_factory=dict)
    source_to_target: dict[str, str] = field(default_factory=dict)
    spec_files: tuple[Path, ...] = ()
    derived_skipped: tuple[str, ...] = ()
    """SDTM targets dropped because they are derived rather than collected."""

    # -- loading ------------------------------------------------------------
    @classmethod
    def load(cls, spec_files: Sequence[Path], include_derived: bool = False) -> SpecCatalog:
        """Read every spec file into one catalogue.

        `include_derived` keeps mappings whose SDTM target is derived or
        assigned (`USUBJID`, `--SEQ`, `EPOCH`, ...). They are dropped by
        default: CDISC conventions document those in define.xml rather than
        annotating them on the CRF, and annotating one is a QC finding.
        """
        catalog = cls(spec_files=tuple(spec_files))
        rows: list[dict[str, str]] = []
        skipped: list[str] = []

        for path in spec_files:
            spec = _read_spec(path)
            if spec is None:
                skipped.append(path.name)
                continue
            rows.extend(catalog._ingest(spec))

        if skipped:
            logger.warning("Skipped %d unusable spec file(s): %s", len(skipped), ", ".join(skipped))

        if rows:
            frame = pd.DataFrame(rows, columns=MAPPING_COLUMNS)
            frame = frame[frame["RSRC_VAR"].astype(bool)]

            if not include_derived:
                derived = frame["TGT_VAR"].map(is_derived_variable)
                if derived.any():
                    dropped = sorted({f"{ds}.{v}" for ds, v in
                                      zip(frame.loc[derived, "TARGET_DS"],
                                          frame.loc[derived, "TGT_VAR"])})
                    catalog.derived_skipped = tuple(dropped)
                    logger.info(
                        "Not annotating %d derived/assigned SDTM target(s) — these "
                        "belong in define.xml with origin Derived or Assigned: %s",
                        len(dropped), ", ".join(dropped[:10]),
                    )
                    frame = frame[~derived]

            catalog.mappings = frame.drop_duplicates().reset_index(drop=True)

        logger.info(
            "Spec catalog: %d mapping rows, %d target domains, %d source datasets",
            len(catalog.mappings), len(catalog.domain_labels), len(catalog.source_to_target),
        )
        return catalog

    def _ingest(self, spec: Mapping[str, Any]) -> list[dict[str, str]]:
        domain = spec.get("domain") or {}
        target = str(domain.get("dataset") or "").strip()
        if not target:
            return []

        label = str(domain.get("label") or "").strip()
        if label:
            self.domain_labels[target] = label
            self.domain_labels.setdefault(strip_supp(target), label)

        source = _source_dataset(domain)
        if not source:
            logger.warning("Spec for %s has no source_dataset; no annotations from it", target)
            return []
        self.source_to_target[source] = target

        rows: list[dict[str, str]] = []
        for variable in spec.get("variables") or ():
            if variable.get("dataset") == target:
                rows.extend(_variable_rows(variable, source, target))

        if _is_supplemental(domain):
            for item in spec.get("value_level") or ():
                row = _supplemental_row(item, source, target)
                if row:
                    rows.append(row)
        else:
            for item in spec.get("value_level") or ():
                if item.get("dataset") == target:
                    rows.extend(_value_level_rows(item, source, target))
        return rows

    # -- lookups ------------------------------------------------------------
    def target_for(self, source_dataset: str) -> Optional[str]:
        """Target dataset for a raw dataset, falling back to a 2-char prefix match."""
        direct = self.source_to_target.get(source_dataset)
        if direct:
            return direct
        prefix = str(source_dataset or "")[:2].upper()
        if not prefix:
            return None
        for dataset in self.domain_labels:
            if dataset.startswith(prefix):
                return dataset
        return None

    def label_for(self, domain: str) -> str:
        return self.domain_labels.get(domain, domain)

    def header_text(self, domain: str) -> str:
        """`AE (Adverse Events)` - the banner drawn at the top of a page."""
        return f"{domain} ({self.label_for(domain)})"


# --- spec file parsing -----------------------------------------------------
def _read_spec(path: Path) -> Optional[Mapping[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            spec = json.load(handle)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in spec %s: %s", path, exc)
        return None
    except OSError as exc:
        logger.error("Cannot read spec %s: %s", path, exc)
        return None

    if not isinstance(spec, Mapping):
        logger.error("Spec %s is not a JSON object", path)
        return None
    if not isinstance(spec.get("domain"), Mapping) or "dataset" not in spec["domain"]:
        logger.warning("Spec %s has no domain.dataset", path)
        return None
    if not isinstance(spec.get("variables", []), list):
        logger.warning("Spec %s has a non-list variables section", path)
        return None
    return spec


def _source_dataset(domain: Mapping[str, Any]) -> str:
    for function in domain.get("functions") or ():
        if isinstance(function, Mapping) and function.get("source_dataset"):
            return str(function["source_dataset"])
    return ""


def _is_supplemental(domain: Mapping[str, Any]) -> bool:
    return (
        str(domain.get("dataset", "")).startswith(SUPP_PREFIX)
        and domain.get("class") == "RELATIONSHIP"
        and _SUPP_LABEL_MARKER in str(domain.get("label", ""))
    )


def _parameters(functions: Any) -> Iterable[tuple[str, Any]]:
    """Yield (name, value) for every parameter of every function."""
    for function in functions or ():
        if not isinstance(function, Mapping):
            continue
        for parameter in function.get("parameters") or ():
            if isinstance(parameter, Mapping):
                yield str(parameter.get("name") or ""), parameter.get("value")


def _split_vars(value: Any) -> list[str]:
    """Normalise a parameter value into a list of source variable names."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip().strip('[]"\' ') for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        # In a list every entry is already one name, and some EDCs use the
        # label as the field name: "Myocardial infarction, inferior" must not
        # be split at its comma. Only a single string is a comma-joined list.
        names: list[str] = []
        for item in value:
            if isinstance(item, str):
                # exact names: only whitespace is trimmed, so a name that ends
                # in a quote (Patient must be classified "operable") survives
                if item.strip():
                    names.append(item.strip())
            else:
                names.extend(_split_vars(item))
        return names
    return [str(value)]


def _input_variables(functions: Any) -> list[str]:
    """Source variables feeding a mapping.

    Prefers `invar*` parameters. The old `extract_source_variable` returned the
    first parameter of the first function whatever its name, so a literal from
    `inval_filter` could end up recorded as a CRF variable and quietly match
    nothing.
    """
    preferred = [v for name, value in _parameters(functions)
                 if name.startswith("invar") for v in _split_vars(value)]
    if preferred:
        return list(dict.fromkeys(preferred))
    # No invar parameter: fall back to the first parameter of any kind.
    fallback = [v for _, value in _parameters(functions) for v in _split_vars(value)]
    return fallback[:1]


def _variable_rows(variable: Mapping[str, Any], source: str, target: str) -> list[dict[str, str]]:
    name = str(variable.get("variable") or "").strip()
    if not name:
        return []
    annotation = f"{target}.{name}"
    return [
        {"RSRC_DS": source, "RSRC_VAR": var, "TARGET_DS": target,
         "TGT_VAR": name, "ATEXT": annotation}
        for var in _input_variables(variable.get("functions"))
    ]


def _supplemental_row(item: Mapping[str, Any], source: str, supp_dataset: str) -> Optional[dict[str, str]]:
    if item.get("variable") != "QVAL":
        return None
    where = str(item.get("where_clause") or "")
    if "QNAM EQ" not in where:
        return None
    qnam = where.split("QNAM EQ", 1)[1].strip().strip('"').strip("'")
    if not qnam:
        return None
    sources = [v for name, value in _parameters(item.get("functions"))
               if name.startswith("invar") for v in _split_vars(value)]
    source_var = sources[0] if sources else qnam
    return {
        "RSRC_DS": source,
        "RSRC_VAR": source_var,
        "TARGET_DS": supp_dataset,
        "TGT_VAR": "QVAL",
        # The CDISC metadata submission guideline's form: short enough to sit
        # beside its question even in a narrow column.
        "ATEXT": f"{qnam} in {supp_dataset}",
    }


def _value_level_rows(item: Mapping[str, Any], source: str, target: str) -> list[dict[str, str]]:
    where = str(item.get("where_clause") or "")
    if " EQ " not in where:
        return []
    condition_var, _, condition_value = where.partition(" EQ ")
    condition_var = condition_var.strip()
    condition_value = condition_value.strip().strip('"').strip("'")
    name = str(item.get("variable") or "").strip()
    if not name or not condition_var:
        return []

    annotation = f"{target}.{name} where {condition_var} = {condition_value}"
    rows = [
        {"RSRC_DS": source, "RSRC_VAR": var, "TARGET_DS": target,
         "TGT_VAR": name, "ATEXT": annotation}
        for var in _input_variables(item.get("functions"))
    ]
    if not rows:
        # Assigned-value mappings carry no source variable, so there is no CRF
        # field to attach them to. Same as before - recorded here so the
        # omission is visible in the log rather than silent.
        logger.debug("Value-level mapping %s has no input variable; not annotated", annotation)
    return rows
