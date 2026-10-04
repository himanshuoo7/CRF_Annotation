"""Join CRF text items to EDC field definitions and then to SDTM annotations.

All three joins are vectorised. Expanding one row per mapping is a left join,
and writing it as one keeps the cost linear in the number of mappings rather
than quadratic.
"""

from __future__ import annotations

import pandas as pd

from .binding import bind_forms, normalise
from .design import StudyDesign
from .logging_utils import get_logger
from .specs import SpecCatalog, domain_of

logger = get_logger()

_HTML_TAG = r"<[^>]*>"
_LINE_BREAK = r"(?i)<\s*/?\s*br\s*/?\s*>"

def build_form_fields(design: StudyDesign) -> pd.DataFrame:
    """One row per (form, field label line), ready to match against CRF text."""
    fields = design.field_frame.copy()
    if fields.empty:
        return pd.DataFrame(columns=["DS_OID", "VAR_OID", "VAR_TXT"])

    # Line breaks become real newlines first, then any other markup is dropped;
    # doing it in this order stops `<br>` from being swallowed by the tag strip.
    text = fields["VAR_TXT"].fillna("").astype(str)
    text = text.str.replace(_LINE_BREAK, "\n", regex=True)
    text = text.str.replace(_HTML_TAG, "", regex=True)
    fields["VAR_TXT"] = text

    fields = fields.assign(VAR_TXT=fields["VAR_TXT"].str.split("\n")).explode("VAR_TXT")
    fields["VAR_TXT"] = fields["VAR_TXT"].fillna("").str.replace(";", "", regex=False)
    fields = fields[fields["VAR_TXT"].str.strip() != ""]

    logger.info("Form/field catalogue: %d label lines across %d forms",
                len(fields), fields["DS_OID"].nunique())
    return fields.reset_index(drop=True)


def _normalise(series: pd.Series) -> pd.Series:
    """Lower-case, collapse whitespace, strip decorative punctuation."""
    return series.fillna("").astype(str).map(normalise)


def match_fields(
    items: pd.DataFrame,
    form_fields: pd.DataFrame,
    design: StudyDesign,
    binder=None,
) -> pd.DataFrame:
    """Left-join CRF text to EDC field definitions on (form OID, label).

    The join key is the *form OID* resolved by `binding`, not the form name
    printed on the page, so it no longer depends on any one EDC's blank-CRF
    conventions.
    """
    bound = bind_forms(items, design, binder)
    if bound.empty or form_fields.empty:
        return bound.assign(DS_OID=bound.get("FORM_OID", ""),
                            DS_NAME=bound.get("FORM_LABEL", ""),
                            VAR_OID=pd.NA, VAR_TXT=pd.NA)

    left = bound.assign(MATCH_TEXT=_normalise(bound["ITM_TEXT"]))
    right = form_fields.assign(MATCH_TEXT=_normalise(form_fields["VAR_TXT"]))
    right = right[right["MATCH_TEXT"] != ""]

    # A form may define the same label twice (repeating rows on a log page).
    # Keeping every copy would stamp identical annotations on top of each other.
    before = len(right)
    right = right.drop_duplicates(subset=["DS_OID", "MATCH_TEXT"], keep="first")
    if before != len(right):
        logger.info("Collapsed %d duplicate form/label definitions", before - len(right))

    merged = left.merge(
        right[["DS_OID", "MATCH_TEXT", "VAR_OID", "VAR_TXT"]],
        left_on=["FORM_OID", "MATCH_TEXT"],
        right_on=["DS_OID", "MATCH_TEXT"],
        how="left",
    )
    merged["DS_OID"] = merged["FORM_OID"]
    merged["DS_NAME"] = merged["FORM_LABEL"]
    merged["MATCH_METHOD"] = merged["VAR_OID"].notna().map({True: "exact", False: ""})
    merged["MATCH_CONFIDENCE"] = merged["VAR_OID"].notna().map({True: 1.0, False: 0.0})

    matched = int(merged["VAR_OID"].notna().sum())
    logger.info("Matched %d of %d CRF text items to EDC fields (exact)", matched, len(merged))
    return merged.drop(columns=["MATCH_TEXT"]).reset_index(drop=True)


def dataset_key(name: object) -> str:
    """Normalise a dataset reference for comparison.

    Spec files name their source as the EDC form (`DM_FORM`) or with a layer
    prefix (`RAW.DM`, `PRECONF.AE`). Only the final segment identifies the
    form, and case is not meaningful.
    """
    text = str(name or "").strip()
    if not text:
        return ""
    return text.rsplit(".", 1)[-1].strip().upper()


def apply_annotations(
    matched: pd.DataFrame,
    catalog: SpecCatalog,
    dataset_scope: str = "auto",
) -> pd.DataFrame:
    """Expand each matched item into one row per SDTM annotation it carries.

    `dataset_scope` controls how a mapping is keyed:

    ``strict``
        Match on (source dataset, variable). A variable named on two forms
        gets only its own form's annotation.
    ``off``
        Match on variable name alone. A variable named on two forms picks up
        both forms' annotations, which is rarely what you want.
    ``auto`` (default)
        Use ``strict`` when the spec source datasets actually line up with the
        EDC form OIDs, and fall back to ``off`` with a loud warning when they
        do not — some builds name them differently and a strict join would
        silently annotate nothing.
    """
    result = matched.copy()
    if result.empty:
        return result.assign(ATEXT="", TARGET="")

    mappings = catalog.mappings
    if mappings.empty:
        logger.warning("No mappings in the spec catalogue; nothing will be annotated")
        return result.assign(ATEXT="", TARGET="")

    scope = str(dataset_scope or "auto").lower()
    if scope not in {"auto", "strict", "off"}:
        logger.warning("Unknown dataset_scope %r; using 'auto'", dataset_scope)
        scope = "auto"

    lookup = mappings[["RSRC_DS", "RSRC_VAR", "ATEXT"]].drop_duplicates().copy()
    lookup["_DS_KEY"] = lookup["RSRC_DS"].map(dataset_key)
    result["_DS_KEY"] = result["FORM_OID"].map(dataset_key) if "FORM_OID" in result.columns \
        else result["DS_OID"].map(dataset_key)

    if scope == "auto":
        scope = _choose_scope(result, lookup)

    if scope == "strict":
        joined = result.merge(
            lookup[["_DS_KEY", "RSRC_VAR", "ATEXT"]],
            left_on=["_DS_KEY", "VAR_OID"],
            right_on=["_DS_KEY", "RSRC_VAR"],
            how="left",
        )
    else:
        _warn_on_ambiguous_variables(mappings)
        joined = result.merge(
            lookup[["RSRC_VAR", "ATEXT"]].drop_duplicates(),
            left_on="VAR_OID", right_on="RSRC_VAR", how="left",
        )

    joined = joined.drop(columns=[c for c in ("RSRC_VAR", "_DS_KEY") if c in joined.columns])
    joined["ATEXT"] = joined["ATEXT"].fillna("")
    joined["TARGET"] = joined["ATEXT"].map(domain_of)

    annotated = int((joined["ATEXT"] != "").sum())
    logger.info("Produced %d annotation rows over %d CRF items (dataset scope: %s)",
                annotated, len(matched), scope)
    return joined.reset_index(drop=True)


def _choose_scope(result: pd.DataFrame, lookup: pd.DataFrame) -> str:
    """Decide whether the spec's source datasets line up with the EDC forms."""
    pairs = set(zip(lookup["_DS_KEY"], lookup["RSRC_VAR"]))
    variables = set(lookup["RSRC_VAR"])

    candidates = result[result["VAR_OID"].notna()]
    if candidates.empty:
        return "strict"

    qualified = sum(
        1 for ds, var in zip(candidates["_DS_KEY"], candidates["VAR_OID"])
        if (ds, var) in pairs
    )
    loose = sum(1 for var in candidates["VAR_OID"] if var in variables)

    if loose == 0:
        return "strict"
    if qualified >= loose:
        logger.info("Spec source datasets align with the EDC forms; "
                    "matching on (dataset, variable)")
        return "strict"

    logger.warning(
        "Spec source datasets do not line up with the EDC form OIDs "
        "(%d of %d matches survive a dataset-qualified join). Falling back to "
        "matching on variable name alone, so a variable defined on two forms "
        "will receive both forms' annotations. Set matching.dataset_scope to "
        "'strict' once the spec source_dataset values match the form OIDs.",
        qualified, loose,
    )
    return "off"


def _warn_on_ambiguous_variables(mappings: pd.DataFrame) -> None:
    """Flag variable names that several source datasets map differently."""
    grouped = mappings.groupby("RSRC_VAR")["RSRC_DS"].nunique()
    ambiguous = grouped[grouped > 1]
    if not ambiguous.empty:
        logger.warning(
            "%d variable name(s) are defined by more than one source dataset "
            "(annotations from all of them will be applied): %s",
            len(ambiguous), ", ".join(sorted(ambiguous.index[:10])),
        )


def unmapped_variables(merged: pd.DataFrame) -> pd.DataFrame:
    """EDC fields that appear on the CRF but carry no SDTM annotation."""
    columns = ["RSRC_DS", "RSRC_VAR", "ATEXT", "PAGE_NO"]
    if merged.empty or "VAR_OID" not in merged.columns:
        return pd.DataFrame(columns=columns)

    annotated = merged["ATEXT"].fillna("").astype(str) != ""
    has_variable = merged["VAR_OID"].fillna("").astype(str).str.len() > 2
    has_form = merged["DS_OID"].fillna("").astype(str).str.len() > 0

    # A variable counts as unmapped only if *no* occurrence of it is annotated.
    annotated_vars = set(merged.loc[annotated, "VAR_OID"].dropna())
    candidates = merged[~annotated & has_variable & has_form]
    candidates = candidates[~candidates["VAR_OID"].isin(annotated_vars)]

    if candidates.empty:
        logger.info("No unmapped variables")
        return pd.DataFrame(columns=columns)

    unmapped = (
        candidates[["DS_OID", "VAR_OID", "PAGE_NO"]]
        .drop_duplicates()
        .rename(columns={"DS_OID": "RSRC_DS", "VAR_OID": "RSRC_VAR"})
        .assign(ATEXT="Not mapped")[columns]
        .sort_values(["PAGE_NO", "RSRC_DS", "RSRC_VAR"])
        .reset_index(drop=True)
    )
    logger.info("Found %d unmapped variable occurrence(s)", len(unmapped))
    return unmapped
