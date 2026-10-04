"""Drawing the annotation boxes onto the CRF.

The old renderer sized each box from its own text-width estimate
(`calculate_text_dimensions`) instead of the rectangle the layout pass had
reserved, using a different characters-per-point constant than the layout code
- so boxes routinely overflowed the gap that was measured for them. Here the
reserved rectangle is authoritative and text is only centred inside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

import pandas as pd
import pymupdf

from .constants import (
    ANNOTATION_HEADER,
    BORDER_COLOR,
    DOMAIN_COLORS,
    EMPTY_POS,
    FIELD_FONT_SIZE,
    FONT_COLOR,
    HEADER_FONT_SIZE,
    HEADER_LEFT_MARGIN,
    HEADER_ROW_HEIGHT,
    HEADER_TOP_MARGIN,
    NOT_SUBMITTED_COLOR,
    NOT_SUBMITTED_TEXT,
)
from .logging_utils import get_logger
from .specs import domain_of

logger = get_logger()

Rect = tuple[float, float, float, float]

_BORDER_WIDTH = 1.0
_DOTTED = [2, 2]
_SOLID: list[int] = []


def parse_pos(value: object) -> Optional[Rect]:
    """Parse a `[x0, y0, x1, y1]` string. Never `eval`.

    This field is read straight out of a user-editable spreadsheet, so it is
    parsed explicitly and never evaluated.
    """
    text = str(value or "").strip()
    if not text or text == EMPTY_POS:
        return None
    try:
        parts = [float(part) for part in text.strip("[]").split(",")]
    except ValueError:
        logger.warning("Unparseable coordinate %r; skipping annotation", text)
        return None
    if len(parts) != 4:
        logger.warning("Coordinate %r does not have four values; skipping", text)
        return None
    x0, y0, x1, y1 = parts
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def annotation_domain(row: Mapping[str, object]) -> str:
    """Domain a row is coloured by."""
    text = str(row.get("A_TEXT") or "")
    if row.get("ANNOTATION_TYPE") == ANNOTATION_HEADER:
        return text.split(" ", 1)[0].strip()
    return domain_of(text)


class CrfRenderer:
    """Writes annotations from a coordinate table onto a PDF."""

    def __init__(self, source_pdf: Path, output_pdf: Path) -> None:
        self.source_pdf = Path(source_pdf)
        self.output_pdf = Path(output_pdf)

    def render(self, coordinates: pd.DataFrame) -> Path:
        if not self.source_pdf.is_file():
            raise FileNotFoundError(f"PDF to annotate not found: {self.source_pdf}")

        by_page = {int(page): rows for page, rows in coordinates.groupby("PAGE_NO", sort=True)}
        drawn = 0
        not_submitted = 0

        with pymupdf.open(self.source_pdf) as document:
            for index, page in enumerate(document, start=1):
                rows = by_page.get(index)
                page_height = float(page.rect.height)
                if rows is None or rows.empty:
                    self._draw_not_submitted(page)
                    not_submitted += 1
                    continue

                palette = self._page_palette(rows)
                page_drawn = 0
                for _, row in rows.iterrows():
                    if self._draw_row(page, row, page_height, palette):
                        page_drawn += 1
                if page_drawn == 0:
                    self._draw_not_submitted(page)
                    not_submitted += 1
                drawn += page_drawn

            document.save(self.output_pdf, deflate=True)

        logger.info("Annotated %s: %d boxes drawn, %d page(s) marked NOT SUBMITTED",
                    self.output_pdf.name, drawn, not_submitted)
        return self.output_pdf

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _page_palette(rows: pd.DataFrame) -> dict[str, tuple[float, float, float]]:
        """Stable colour per domain, in order of first appearance on the page."""
        palette: dict[str, tuple[float, float, float]] = {}
        for _, row in rows.iterrows():
            domain = annotation_domain(row)
            if domain and domain not in palette:
                palette[domain] = DOMAIN_COLORS[len(palette) % len(DOMAIN_COLORS)]
        return palette

    def _draw_row(
        self,
        page: pymupdf.Page,
        row: Mapping[str, object],
        page_height: float,
        palette: Mapping[str, tuple[float, float, float]],
    ) -> bool:
        text = str(row.get("A_TEXT") or "").strip()
        if not text:
            return False
        rect = parse_pos(row.get("POS1"))
        if rect is None:
            return False

        is_header = row.get("ANNOTATION_TYPE") == ANNOTATION_HEADER
        x0, y0, x1, y1 = self._to_pdf_space(rect, page_height, is_header)
        colour = palette.get(annotation_domain(row), DOMAIN_COLORS[0])
        return self._add_box(page, text, (x0, y0, x1, y1), colour, is_header)

    @staticmethod
    def _to_pdf_space(rect: Rect, page_height: float, is_header: bool) -> Rect:
        """Bottom-left origin -> PyMuPDF's top-left origin.

        Header rectangles written by older versions of this tool were stored in
        top-left coordinates (y around 30-45); they are detected and passed
        through instead of being flipped to the foot of the page.
        """
        x0, y0, x1, y1 = rect
        if is_header and y1 <= page_height / 2:
            return x0, y0, x1, y1
        return x0, page_height - y1, x1, page_height - y0

    def _draw_not_submitted(self, page: pymupdf.Page) -> None:
        rect = (
            HEADER_LEFT_MARGIN,
            HEADER_TOP_MARGIN,
            HEADER_LEFT_MARGIN + 120.0,
            HEADER_TOP_MARGIN + HEADER_ROW_HEIGHT,
        )
        self._add_box(page, NOT_SUBMITTED_TEXT, rect, NOT_SUBMITTED_COLOR,
                      is_header=True, dashes=_DOTTED)

    @staticmethod
    def _add_box(
        page: pymupdf.Page,
        text: str,
        rect: Rect,
        colour: tuple[float, float, float],
        is_header: bool,
        dashes: Optional[Sequence[int]] = None,
    ) -> bool:
        font_size = HEADER_FONT_SIZE if is_header else FIELD_FONT_SIZE
        x0, top, x1, bottom = rect
        # Centre the text band inside the reserved rectangle.
        text_height = font_size * 1.35
        offset = max(0.0, (bottom - top - text_height) / 2)
        box = pymupdf.Rect(x0, top + offset, x1, bottom - offset)
        if box.is_empty or box.is_infinite:
            return False

        try:
            annot = _create_freetext(page, box, text, font_size, colour)
            annot.set_border(width=_BORDER_WIDTH, dashes=list(dashes) if dashes else _SOLID)
            annot.set_info(title=text)
            _update_freetext(annot)
        except Exception as exc:  # noqa: BLE001 - one bad box must not kill the run
            logger.warning("Could not draw annotation %r at %s: %s", text, rect, exc)
            return False
        return True


# --- PyMuPDF compatibility -------------------------------------------------
# `border_color` on a FreeText annotation became rich-text-only, and
# `Annot.update(text_align=...)` was dropped, in recent PyMuPDF releases. The
# original code used both unconditionally inside a bare `except: return None`,
# so on a newer PyMuPDF it produced a PDF with no annotations at all and said
# nothing. Here the supported call shape is probed once and reused.
_CREATE_MODE: Optional[int] = None
_UPDATE_ALIGN: Optional[bool] = None


def _create_freetext(page, box, text: str, font_size: float, colour):
    global _CREATE_MODE
    base = {
        "fontsize": font_size,
        "text_color": FONT_COLOR,
        "fill_color": colour,
        "align": pymupdf.TEXT_ALIGN_CENTER,
        "rotate": 0,
        "fontname": "helv",
    }
    variants = (
        {**base, "border_color": BORDER_COLOR},   # classic API
        {**base, "border_width": _BORDER_WIDTH},  # PyMuPDF >= 1.25
        base,                                     # last resort: no border args
    )
    order = range(len(variants)) if _CREATE_MODE is None else (_CREATE_MODE,)
    last: Exception | None = None
    for index in order:
        try:
            annot = page.add_freetext_annot(box, text, **variants[index])
        except (TypeError, ValueError) as exc:
            last = exc
            continue
        _CREATE_MODE = index
        return annot
    raise last if last else RuntimeError("add_freetext_annot failed")


def _update_freetext(annot) -> None:
    global _UPDATE_ALIGN
    if _UPDATE_ALIGN is not False:
        try:
            annot.update(text_align=pymupdf.TEXT_ALIGN_CENTER)
            _UPDATE_ALIGN = True
            return
        except TypeError:
            _UPDATE_ALIGN = False
    annot.update()
