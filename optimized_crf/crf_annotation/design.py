"""The canonical study design — the seam between "any EDC" and the pipeline.

Every EDC describes the same four things: forms, the fields on them, visits,
and which forms are collected at which visit. They disagree only on file
format and vocabulary. Adapters in `crf_annotation.sources` translate a vendor
export into this model; nothing downstream of here knows or cares which EDC
the study was built in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import pandas as pd

from .logging_utils import get_logger

logger = get_logger()

FORM_COLUMNS = ["DS_OID", "DS_NAME"]
FIELD_COLUMNS = ["DS_OID", "VAR_OID", "VAR_TXT"]
VISIT_COLUMNS = ["DS_OID", "FDR_OID", "VISIT", "VISITNUM", "FORM"]


class DesignError(ValueError):
    """Raised when a study design cannot be read or is unusable."""


@dataclass(frozen=True)
class Form:
    """A CRF page definition. `oid` is whatever the EDC calls its stable key."""

    oid: str
    label: str
    order: int = 0
    repeating: bool = False
    source_oid: str = ""
    """The vendor's own key, when it differs from `oid`. Traceability only."""


@dataclass(frozen=True)
class Field:
    """One captured item on a form. `label` is the text printed on the CRF."""

    oid: str
    """The name the SDTM mapping specs refer to this field by."""
    form_oid: str
    label: str
    order: int = 0
    datatype: str = ""
    codelist_oid: str = ""
    source_oid: str = ""
    """The vendor's own key, when it differs from `oid`. Traceability only."""


@dataclass(frozen=True)
class Visit:
    """A study event / folder / visit."""

    oid: str
    label: str
    order: int = 0
    parent_oid: str = ""


@dataclass(frozen=True)
class VisitForm:
    """An entry in the visit-by-form matrix."""

    visit_oid: str
    form_oid: str


@dataclass(frozen=True)
class StudyDesign:
    """Normalised study design, whatever system it came from."""

    source_system: str
    forms: tuple[Form, ...] = ()
    fields: tuple[Field, ...] = ()
    visits: tuple[Visit, ...] = ()
    visit_forms: tuple[VisitForm, ...] = ()
    study_name: str = ""
    warnings: tuple[str, ...] = ()

    # -- validation ---------------------------------------------------------
    def validate(self) -> StudyDesign:
        """Reject a design that cannot produce annotations, warn about the rest."""
        if not self.forms:
            raise DesignError(f"{self.source_system}: study design contains no forms")
        if not self.fields:
            raise DesignError(f"{self.source_system}: study design contains no fields")

        form_oids = {form.oid for form in self.forms}
        orphans = sorted({f.form_oid for f in self.fields if f.form_oid not in form_oids})
        if orphans:
            logger.warning(
                "%s: %d field(s) reference %d unknown form(s): %s",
                self.source_system, sum(f.form_oid in orphans for f in self.fields),
                len(orphans), ", ".join(orphans[:5]),
            )

        unlabelled = sum(1 for f in self.fields if not f.label.strip())
        if unlabelled:
            logger.warning("%s: %d field(s) have no label and can never match CRF text",
                           self.source_system, unlabelled)
        for warning in self.warnings:
            logger.warning("%s: %s", self.source_system, warning)

        logger.info(
            "Study design from %s: %d forms, %d fields, %d visits, %d visit/form pairs",
            self.source_system, len(self.forms), len(self.fields),
            len(self.visits), len(self.visit_forms),
        )
        return self

    # -- frames consumed by the pipeline ------------------------------------
    @property
    def form_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [{"DS_OID": f.oid, "DS_NAME": f.label} for f in self.forms],
            columns=FORM_COLUMNS,
        ).astype(str)

    @property
    def field_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [{"DS_OID": f.form_oid, "VAR_OID": f.oid, "VAR_TXT": f.label} for f in self.fields],
            columns=FIELD_COLUMNS,
        ).astype(str)

    @property
    def visit_frame(self) -> pd.DataFrame:
        """Form-per-visit rows, ready for the Visits section of the outline."""
        if not self.visit_forms:
            return pd.DataFrame(columns=VISIT_COLUMNS)
        visits = {v.oid: v for v in self.visits}
        forms = {f.oid: f for f in self.forms}
        rows = []
        for pair in self.visit_forms:
            visit = visits.get(pair.visit_oid)
            form = forms.get(pair.form_oid)
            rows.append(
                {
                    "DS_OID": pair.form_oid,
                    "FDR_OID": pair.visit_oid,
                    "VISIT": visit.label if visit else pair.visit_oid,
                    "VISITNUM": visit.order if visit else 0,
                    "FORM": form.label if form else pair.form_oid,
                }
            )
        return (
            pd.DataFrame(rows, columns=VISIT_COLUMNS)
            .drop_duplicates()
            .sort_values(["VISITNUM", "DS_OID"], kind="stable")
            .reset_index(drop=True)
        )

    @property
    def form_labels(self) -> dict[str, str]:
        """Form OID -> label, with the SDTM SUPP prefix stripped."""
        labels: dict[str, str] = {}
        for form in self.forms:
            oid = form.oid.strip()
            label = form.label.strip()
            if not oid or not label:
                continue
            labels[oid] = label
            if oid.startswith("SUPP"):
                labels.setdefault(oid[4:], label)
        return labels


def build(
    source_system: str,
    forms: Iterable[Form],
    fields: Iterable[Field],
    visits: Iterable[Visit] = (),
    visit_forms: Iterable[VisitForm] = (),
    study_name: str = "",
    warnings: Sequence[str] = (),
) -> StudyDesign:
    """Convenience constructor for adapters."""
    return StudyDesign(
        source_system=source_system,
        forms=tuple(forms),
        fields=tuple(fields),
        visits=tuple(visits),
        visit_forms=tuple(visit_forms),
        study_name=study_name,
        warnings=tuple(warnings),
    )
