"""The aCRF config workbook: the reviewable record of what gets annotated.

Written once, with its final column names. The display names and the internal
names are a single invertible mapping, so a workbook round-trips without
losing a column.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

import pandas as pd

from .constants import (
    ANNOTATION_FIELD,
    ANNOTATION_FORM,
    ANNOTATION_HEADER,
    COORDINATE_COLUMNS,
    COORDINATE_SHEET,
    DISPLAY_TO_INTERNAL,
    EMPTY_POS,
    FILENAMES_SHEET,
    FORM_PREFIX,
    HEADER_CHAR_WIDTH,
    HEADER_GAP_X,
    HEADER_GAP_Y,
    HEADER_LEFT_MARGIN,
    HEADER_RIGHT_MARGIN,
    HEADER_ROW_HEIGHT,
    HEADER_TOP_MARGIN,
    INTERNAL_TO_DISPLAY,
    UNMAPPED_SHEET,
)
from .logging_utils import get_logger
from .pdf_text import PageGeometry, format_pos
from .specs import SpecCatalog, domain_of

logger = get_logger()


class ConfigWorkbookError(ValueError):
    """Raised when a config workbook cannot be used to drive a rerun."""


def header_width(text: str) -> float:
    return len(str(text)) * HEADER_CHAR_WIDTH + 2 * HEADER_GAP_X


def header_rows_for_page(page_no: int, texts: Sequence[str], page_width: float, page_height: float,
                         ds_oid: object = "", itm_text: object = "", ds_name: object = "") -> list[dict[str, object]]:
    """Domain banners for one page, laid out left to right with wrap."""
    rows: list[dict[str, object]] = []
    x = HEADER_LEFT_MARGIN
    top = page_height - HEADER_TOP_MARGIN
    for text in texts:
        width = header_width(text)
        if x + width > page_width - HEADER_RIGHT_MARGIN and x > HEADER_LEFT_MARGIN:
            x = HEADER_LEFT_MARGIN
            top -= HEADER_ROW_HEIGHT + HEADER_GAP_Y
        rows.append({
            "PAGE_NO": page_no, "DS_OID": ds_oid, "ITM_TEXT": itm_text, "DS_NAME": ds_name,
            "POS1": format_pos(x, top - HEADER_ROW_HEIGHT, x + width, top),
            "A_TEXT": text, "ANNOTATION_TYPE": ANNOTATION_HEADER,
        })
        x += width + HEADER_GAP_X
    return rows


def build_header_rows(
    merged: pd.DataFrame,
    catalog: SpecCatalog,
    geometry: PageGeometry,
) -> pd.DataFrame:
    """One banner per (page, target domain), laid out left to right with wrap."""
    rows: list[dict[str, object]] = []
    if merged.empty:
        return pd.DataFrame(rows, columns=list(COORDINATE_COLUMNS))

    annotated = merged[merged["ATEXT"].fillna("").astype(str) != ""]
    if annotated.empty:
        return pd.DataFrame(rows, columns=list(COORDINATE_COLUMNS))

    banners = merged[merged["ITM_TEXT"].str.startswith(FORM_PREFIX.rstrip(), na=False)]
    banner_by_page = banners.drop_duplicates(subset=["PAGE_NO"]).set_index("PAGE_NO")

    for page_no, page_rows in annotated.groupby("PAGE_NO", sort=True):
        domains = sorted({d for d in page_rows["ATEXT"].map(domain_of) if d})
        if not domains:
            continue

        banner = banner_by_page.loc[page_no] if page_no in banner_by_page.index else None
        page_width = geometry.width(int(page_no))
        page_height = geometry.height(int(page_no))

        rows.extend(header_rows_for_page(
            page_no, [catalog.header_text(domain) for domain in domains], page_width, page_height,
            ds_oid="" if banner is None else banner.get("DS_OID", ""),
            itm_text="" if banner is None else banner.get("ITM_TEXT", ""),
            ds_name="" if banner is None else banner.get("DS_NAME", ""),
        ))

    logger.info("Built %d domain header banner(s)", len(rows))
    return pd.DataFrame(rows, columns=list(COORDINATE_COLUMNS))


def build_coordinate_table(
    merged: pd.DataFrame,
    catalog: SpecCatalog,
    geometry: PageGeometry,
) -> pd.DataFrame:
    """Form banners, domain headers and placed field annotations, in page order."""
    headers = build_header_rows(merged, catalog, geometry)

    is_banner = merged["ITM_TEXT"].str.startswith(FORM_PREFIX.rstrip(), na=False)
    banners = _form_rows(merged, is_banner)

    fields = merged[~is_banner].copy()
    fields = fields[fields["POS1"].fillna(EMPTY_POS) != EMPTY_POS]
    fields["A_TEXT"] = fields["ATEXT"]
    fields["ANNOTATION_TYPE"] = ANNOTATION_FIELD
    fields = fields.reindex(columns=list(COORDINATE_COLUMNS))

    table = pd.concat([banners, headers, fields], ignore_index=True)
    order = {ANNOTATION_FORM: 0, ANNOTATION_HEADER: 1, ANNOTATION_FIELD: 2}
    table = (
        table.assign(_rank=table["ANNOTATION_TYPE"].map(order).fillna(3))
        .sort_values(["PAGE_NO", "_rank"], kind="stable")
        .drop(columns=["_rank"])
        .reset_index(drop=True)
    )
    table = table.fillna("")
    logger.info("Coordinate table: %d rows (%d form banners, %d headers, %d fields)",
                len(table), len(banners), len(headers), len(fields))
    return table


def _form_rows(merged: pd.DataFrame, is_banner: pd.Series) -> pd.DataFrame:
    """One `Form` row per (page, form), carrying page/form identity only.

    The workbook needs a complete form -> page map, including pages resolved
    without a printed banner and pages with nothing submitted. Where the CRF
    does print a banner we reuse that row; otherwise the row is synthesised, so
    an ordinary annotated field is never mislabelled as the page's form banner.
    """
    identified = merged[merged["DS_OID"].astype(str) != ""]
    if identified.empty:
        return pd.DataFrame(columns=list(COORDINATE_COLUMNS))

    # Prefer a real banner row for each (page, form); fall back to any row.
    preferred = identified.assign(_is_banner=is_banner.reindex(identified.index, fill_value=False))
    preferred = (
        preferred.sort_values("_is_banner", ascending=False, kind="stable")
        .drop_duplicates(subset=["PAGE_NO", "DS_OID"], keep="first")
        .sort_index()
    )

    rows = preferred.copy()
    synthesised = ~rows["_is_banner"]
    rows["POS1"] = rows["ITM_DIM"]
    rows.loc[synthesised, "POS1"] = EMPTY_POS
    rows.loc[synthesised, "ITM_TEXT"] = ""
    rows["A_TEXT"] = ""
    rows["ANNOTATION_TYPE"] = ANNOTATION_FORM
    return rows.reindex(columns=list(COORDINATE_COLUMNS))


def write_config_workbook(
    path: Path,
    coordinates: pd.DataFrame,
    unmapped: Optional[pd.DataFrame] = None,
    file_info: Optional[pd.DataFrame] = None,
) -> Path:
    """Write the workbook with display column names, in one pass."""
    display = coordinates.rename(columns=INTERNAL_TO_DISPLAY)
    ordered = [INTERNAL_TO_DISPLAY[c] for c in COORDINATE_COLUMNS if INTERNAL_TO_DISPLAY[c] in display.columns]
    display = display[ordered]

    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        display.to_excel(writer, sheet_name=COORDINATE_SHEET, index=False)
        if unmapped is not None and not unmapped.empty:
            unmapped.to_excel(writer, sheet_name=UNMAPPED_SHEET, index=False)
        if file_info is not None and not file_info.empty:
            file_info.to_excel(writer, sheet_name=FILENAMES_SHEET, index=False)
    logger.info("Wrote config workbook %s (%d coordinate rows)", path, len(display))
    return path


def read_config_workbook(path: Path) -> pd.DataFrame:
    """Read a config workbook back into internal column names.

    Accepts both display names and the internal names older files used.
    """
    try:
        coordinates = pd.read_excel(path, sheet_name=COORDINATE_SHEET, engine="openpyxl")
    except ValueError as exc:
        raise ConfigWorkbookError(f"{path} has no '{COORDINATE_SHEET}' sheet") from exc
    except OSError as exc:
        raise ConfigWorkbookError(f"Cannot read config workbook {path}: {exc}") from exc

    coordinates = coordinates.rename(columns=DISPLAY_TO_INTERNAL)
    # Legacy files carried the annotation under ATEXT instead of A_TEXT.
    if "A_TEXT" not in coordinates.columns and "ATEXT" in coordinates.columns:
        coordinates = coordinates.rename(columns={"ATEXT": "A_TEXT"})
    elif "ATEXT" in coordinates.columns:
        fallback = coordinates["ATEXT"].fillna("")
        coordinates["A_TEXT"] = coordinates["A_TEXT"].fillna("").where(
            coordinates["A_TEXT"].fillna("") != "", fallback
        )
        coordinates = coordinates.drop(columns=["ATEXT"])

    missing = [c for c in ("PAGE_NO", "POS1", "A_TEXT") if c not in coordinates.columns]
    if missing:
        raise ConfigWorkbookError(f"Config workbook {path} is missing column(s): {missing}")

    for column in COORDINATE_COLUMNS:
        if column not in coordinates.columns:
            coordinates[column] = ""
    coordinates = coordinates[list(COORDINATE_COLUMNS)].copy()

    coordinates["PAGE_NO"] = pd.to_numeric(coordinates["PAGE_NO"], errors="coerce")
    coordinates = coordinates[coordinates["PAGE_NO"].notna()]
    coordinates["PAGE_NO"] = coordinates["PAGE_NO"].astype(int)
    for column in ("ITM_TEXT", "DS_OID", "DS_NAME", "A_TEXT", "ANNOTATION_TYPE",
                   "MATCH_METHOD", "MATCH_NOTE"):
        coordinates[column] = coordinates[column].fillna("").astype(str)
    coordinates["MATCH_CONFIDENCE"] = pd.to_numeric(
        coordinates["MATCH_CONFIDENCE"], errors="coerce"
    ).fillna(0.0)
    coordinates["POS1"] = coordinates["POS1"].fillna(EMPTY_POS).astype(str)
    coordinates["ANNOTATION_TYPE"] = coordinates["ANNOTATION_TYPE"].replace("", ANNOTATION_FIELD)

    if coordinates.empty:
        raise ConfigWorkbookError(f"Config workbook {path} has no usable coordinate rows")

    logger.info("Read %d coordinate rows from %s", len(coordinates), path.name)
    return coordinates.reset_index(drop=True)


def build_file_info(names: Mapping[str, Sequence[str]]) -> pd.DataFrame:
    """`{"ALS_File": [...], "Spec_File": [...]}` -> the FileNames sheet."""
    rows = [
        {"File_Type": file_type, "File_Name": name}
        for file_type, values in names.items()
        for name in values
        if name
    ]
    return pd.DataFrame(rows, columns=["File_Type", "File_Name"])
