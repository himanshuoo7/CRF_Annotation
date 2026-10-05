"""Deciding which form each CRF page belongs to.

Blank CRFs disagree about how they identify a form. Medidata Rave prints a
`Form: <name>` banner on every page; Veeva, Viedoc and hand-built CRFs print
the name differently, or only in a header band, or not at all.

Binding is therefore a strategy. `AutoBinder` tries each one and keeps
whichever binds the most pages, so a banner-style CRF resolves through the
banner rule and everything else falls through to name matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

import pandas as pd

from .design import StudyDesign
from .logging_utils import get_logger

logger = get_logger()

BINDING_COLUMNS = ["FORM_OID", "FORM_LABEL"]
_HEADER_BAND = 0.22   # fraction of page height treated as the header zone
_CARRY_EVIDENCE = 2   # field labels a page must share with the form it inherits


# "1.1 ", "2. ", "3) ", "Q4 " - question numbering a CRF prints and an EDC
# label does not carry.
_NUMBERING = re.compile(
    r"^(?:q(?:uestion)?\s*\d{1,3}[.):]?"     # Q4, Question 4)
    r"|\d{1,3}(?:\.\d{1,3})+[.):]?"          # 1.1, 2.3.1.
    r"|\d{1,3}[.):])"                         # 2. 3) 4:  - a bare "2 weeks" is not numbering
    r"\s+(?=\S)"
)


def normalise(text: object) -> str:
    """Lower-case, collapse whitespace, drop question numbering and
    punctuation used decoratively."""
    cleaned = re.sub(r"[\s\u00a0]+", " ", str(text or "").strip().lower())
    cleaned = _NUMBERING.sub("", cleaned)
    return cleaned.strip(" :-–—_.")


class FormBinder(Protocol):
    name: str

    def bind(self, items: pd.DataFrame, design: StudyDesign) -> pd.Series:
        """Return a FORM_OID per row of `items` (empty string where unknown)."""


@dataclass
class BannerBinder:
    """Rave-style: a line that starts with a known prefix names the form."""

    prefix: str = "Form: "
    name: str = "banner prefix"

    def bind(self, items: pd.DataFrame, design: StudyDesign) -> pd.Series:
        prefix = self.prefix.rstrip()
        by_label = {normalise(f.label): f.oid for f in design.forms}
        by_oid = {normalise(f.oid): f.oid for f in design.forms}

        is_banner = items["ITM_TEXT"].str.startswith(prefix, na=False)
        stripped = items["ITM_TEXT"].where(is_banner).str.replace(
            re.escape(self.prefix), "", n=1, regex=True
        )
        resolved = stripped.map(
            lambda text: "" if pd.isna(text)
            else by_label.get(normalise(text), by_oid.get(normalise(text), ""))
        )
        resolved = resolved.where(resolved.astype(bool))
        return items.assign(_oid=resolved).groupby("PAGE_NO")["_oid"].ffill().fillna("")


@dataclass
class HeaderNameBinder:
    """No banner: match text in the page header band against form names."""

    band: float = _HEADER_BAND
    name: str = "header form name"

    def bind(self, items: pd.DataFrame, design: StudyDesign) -> pd.Series:
        by_label = {normalise(f.label): f.oid for f in design.forms}
        by_oid = {normalise(f.oid): f.oid for f in design.forms}
        lookup = {**by_oid, **by_label}

        # Y is bottom-origin, so the header band is the *top* of the range.
        page_top = items.groupby("PAGE_NO")["Y1"].transform("max")
        page_bottom = items.groupby("PAGE_NO")["Y0"].transform("min")
        threshold = page_top - (page_top - page_bottom) * self.band
        in_header = items["Y0"] >= threshold

        candidate = items["ITM_TEXT"].where(in_header).map(
            lambda text: "" if pd.isna(text) else lookup.get(normalise(text), "")
        )
        candidate = candidate.where(candidate.astype(bool))
        # One form per page: take the first header hit, apply it to the page.
        per_page = candidate.groupby(items["PAGE_NO"]).transform("first")
        return per_page.fillna("")


@dataclass
class SequentialBinder:
    """Last resort: pages follow the design's form order, one form per page.

    Only used when nothing else binds anything, and always logged loudly — it
    is a guess, and every annotation it produces should be reviewed.
    """

    name: str = "sequential fallback"

    def bind(self, items: pd.DataFrame, design: StudyDesign) -> pd.Series:
        pages = sorted(items["PAGE_NO"].unique())
        ordered = [f.oid for f in sorted(design.forms, key=lambda f: f.order)]
        mapping = {page: ordered[index] for index, page in enumerate(pages) if index < len(ordered)}
        return items["PAGE_NO"].map(mapping).fillna("")


DEFAULT_BINDERS: tuple[FormBinder, ...] = (BannerBinder(), HeaderNameBinder())


class AutoBinder:
    """Try each binder, keep the one that resolves the most pages."""

    name = "auto"

    def __init__(self, binders: Sequence[FormBinder] = DEFAULT_BINDERS,
                 fallback: Optional[FormBinder] = None) -> None:
        self.binders = tuple(binders)
        self.fallback = fallback

    def bind(self, items: pd.DataFrame, design: StudyDesign) -> pd.Series:
        best_name, best_result, best_pages = "", None, 0
        total_pages = items["PAGE_NO"].nunique()

        for binder in self.binders:
            try:
                result = binder.bind(items, design)
            except Exception as exc:  # noqa: BLE001 - a failing strategy is not fatal
                logger.debug("Binder %s failed: %s", binder.name, exc)
                continue
            bound_pages = items.loc[result.astype(bool), "PAGE_NO"].nunique()
            logger.info("Form binder '%s' resolved %d/%d pages", binder.name, bound_pages, total_pages)
            if bound_pages > best_pages:
                best_name, best_result, best_pages = binder.name, result, bound_pages

        if best_result is None or best_pages == 0:
            if self.fallback is None:
                logger.error(
                    "No form binder could match any CRF page to a form. The CRF's "
                    "form names and the study design's form names do not overlap."
                )
                return pd.Series("", index=items.index, dtype=object)
            logger.warning("Falling back to '%s' binding — every annotation needs review",
                           self.fallback.name)
            return self.fallback.bind(items, design)

        logger.info("Using form binder '%s' (%d/%d pages bound)", best_name, best_pages, total_pages)
        best_result = carry_forward(items, design, best_result)
        best_pages = items.loc[best_result.astype(bool), "PAGE_NO"].nunique()
        if best_pages < total_pages:
            unbound = sorted(set(items["PAGE_NO"]) - set(items.loc[best_result.astype(bool), "PAGE_NO"]))
            logger.info("Pages with no form binding (left unannotated): %s",
                        ", ".join(str(p) for p in unbound[:20]))
        return best_result


def carry_forward(items: pd.DataFrame, design: StudyDesign, bound: pd.Series) -> pd.Series:
    """A page that doesn't name its form continues the page before it.

    Many CRFs print the form name only on a form's first page. An unbound page
    inherits the previous page's form — but only when it shows that form's own
    field labels, so cover sheets, link pages and instructions stay unbound.
    """
    labels: dict[str, set[str]] = {}
    for item in design.fields:
        labels.setdefault(item.form_oid, set()).add(normalise(item.label))

    result = bound.astype(object).copy()
    page_of = items["PAGE_NO"]
    previous = ""
    carried: list[int] = []
    for page in sorted(page_of.unique()):
        rows = page_of == page
        current = next((oid for oid in result[rows] if oid), "")
        if current:
            previous = current
            continue
        if not previous:
            continue
        texts = set(items.loc[rows, "ITM_TEXT"].map(normalise))
        if len(texts & labels.get(previous, set())) >= _CARRY_EVIDENCE:
            result[rows] = previous
            carried.append(int(page))
        else:
            previous = ""  # a gap (cover sheet, links) ends the form
    if carried:
        logger.info("Carried the form over to %d continuation page(s): %s",
                    len(carried), ", ".join(str(p) for p in carried[:20]))
    return result


def bind_forms(
    items: pd.DataFrame,
    design: StudyDesign,
    binder: Optional[FormBinder] = None,
) -> pd.DataFrame:
    """Attach FORM_OID and FORM_LABEL to every extracted text item."""
    if items.empty:
        return items.assign(FORM_OID="", FORM_LABEL="")

    resolved = (binder or AutoBinder()).bind(items, design)
    labels = {f.oid: f.label for f in design.forms}
    return items.assign(
        FORM_OID=resolved.astype(str),
        FORM_LABEL=resolved.map(lambda oid: labels.get(oid, "")).fillna(""),
    )
