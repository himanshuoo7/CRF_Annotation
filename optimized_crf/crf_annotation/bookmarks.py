"""PDF outline: Study > Domains > domain > forms, and Study > Visits > visit > forms.

Everything is built from data already in memory. The old rerun handler read the
same config workbook three times (once per bookmark section plus once for the
page map) and re-globbed and re-parsed every spec JSON on top of that.
"""

from __future__ import annotations

from pathlib import Path
import pandas as pd
from pypdf import PdfReader, PdfWriter, generic
from pypdf.generic import Fit

from .constants import (
    ANNOTATION_FORM,
    ANNOTATION_HEADER,
    DEFAULT_PDF_AUTHOR,
    FORM_PREFIX,
    PDF_KEYWORDS,
)
from .logging_utils import get_logger

logger = get_logger()


def _collapse(writer: PdfWriter, bookmark) -> None:
    """Set /Count to 0 so the node renders collapsed in a viewer."""
    node = (bookmark if bookmark is not None else writer.get_outline_root()).get_object()
    node[generic.NameObject("/Count")] = generic.NumberObject(0)


def _add(writer: PdfWriter, title: str, page: int, parent=None):
    return writer.add_outline_item(
        title=str(title),
        page_number=max(0, int(page)),
        parent=parent,
        color=None,
        bold=False,
        italic=False,
        fit=Fit.fit(),
    )


def form_page_map(coordinates: pd.DataFrame) -> dict[str, int]:
    """Form OID -> 0-based page of its first appearance."""
    banners = coordinates[coordinates["ANNOTATION_TYPE"] == ANNOTATION_FORM]
    if banners.empty:
        # Workbooks written before form rows existed: fall back to the banner
        # text convention they were produced with.
        banners = coordinates[
            coordinates["ITM_TEXT"].str.startswith(FORM_PREFIX.rstrip(), na=False)
        ]
    banners = banners[banners["DS_OID"].astype(str) != ""]
    if banners.empty:
        return {}
    first = banners.groupby("DS_OID")["PAGE_NO"].min()
    return {str(oid): max(0, int(page) - 1) for oid, page in first.items()}


def _domain_sections(coordinates: pd.DataFrame) -> dict[str, dict[str, object]]:
    """Domain title -> {page, forms:[(page, title)]} taken from header banners."""
    headers = coordinates[
        (coordinates["ANNOTATION_TYPE"] == ANNOTATION_HEADER)
        & (coordinates["A_TEXT"].astype(str) != "")
    ]
    sections: dict[str, dict[str, object]] = {}
    for _, row in headers.iterrows():
        title = str(row["A_TEXT"]).strip()
        page = max(0, int(row["PAGE_NO"]) - 1)
        section = sections.setdefault(title, {"page": page, "forms": []})
        section["page"] = min(int(section["page"]), page)

        form_title = str(row.get("DS_NAME") or "").strip()
        if not form_title:
            form_title = str(row.get("ITM_TEXT") or "").replace(FORM_PREFIX, "").strip()
        if form_title:
            forms = section["forms"]
            if (page, form_title) not in forms:
                forms.append((page, form_title))
    return sections


def add_outline(
    source_pdf: Path,
    output_pdf: Path,
    coordinates: pd.DataFrame,
    visit_matrix: pd.DataFrame,
    study_name: str,
    author: str = "",
) -> Path:
    """Write `source_pdf` back out to `output_pdf` with the full outline."""
    reader = PdfReader(str(source_pdf))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    writer.add_metadata(
        {
            "/Author": author or DEFAULT_PDF_AUTHOR,
            "/Keywords": PDF_KEYWORDS,
            "/Subject": study_name,
            "/Title": f"SDTM Annotated CRF: {study_name}",
        }
    )

    study = _add(writer, study_name, 0)
    _collapse(writer, study)

    _add_domain_bookmarks(writer, study, coordinates)
    _add_visit_bookmarks(writer, study, coordinates, visit_matrix)

    with open(output_pdf, "wb") as handle:
        writer.write(handle)
    logger.info("Outline written to %s", Path(output_pdf).name)
    return Path(output_pdf)


def _add_domain_bookmarks(writer: PdfWriter, study, coordinates: pd.DataFrame) -> None:
    sections = _domain_sections(coordinates)
    if not sections:
        logger.info("No domain headers; skipping domain bookmarks")
        return

    parent = _add(writer, "Domains", 0, study)
    _collapse(writer, parent)

    for title in sorted(sections):
        section = sections[title]
        node = _add(writer, title, int(section["page"]), parent)
        _collapse(writer, node)
        for page, form_title in sorted(section["forms"]):
            _add(writer, form_title, page, node)
    logger.info("Added %d domain bookmark(s)", len(sections))


def _add_visit_bookmarks(
    writer: PdfWriter,
    study,
    coordinates: pd.DataFrame,
    visit_matrix: pd.DataFrame,
) -> None:
    if visit_matrix is None or visit_matrix.empty:
        logger.info("No visit matrix; skipping visit bookmarks")
        return

    pages = form_page_map(coordinates)
    parent = _add(writer, "Visits", 0, study)
    _collapse(writer, parent)

    ordered = visit_matrix.sort_values(["VISITNUM", "DS_OID"], kind="stable")
    count = 0
    for visit_name, forms in ordered.groupby("VISIT", sort=False):
        form_ids = [str(oid) for oid in dict.fromkeys(forms["DS_OID"])]
        first_page = next((pages[oid] for oid in form_ids if oid in pages), 0)
        node = _add(writer, visit_name, first_page, parent)
        _collapse(writer, node)

        titles = dict(zip(forms["DS_OID"].astype(str), forms["FORM"].astype(str)))
        for oid in form_ids:
            _add(writer, titles.get(oid, oid), pages.get(oid, 0), node)
        count += 1
    logger.info("Added %d visit bookmark(s)", count)
