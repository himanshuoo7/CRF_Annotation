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
from .layout import ANNOTATION_FONT, THREE_LINE_HEIGHT, TWO_LINE_HEIGHT, three_lines, two_lines
from .specs import domain_of

logger = get_logger()

Rect = tuple[float, float, float, float]

_BORDER_WIDTH = 1.0
_DOTTED = [2, 2]


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
            # A blank CRF exported from an EDC is often fillable. An aCRF is a
            # document, not a form, and the FDA PDF specification allows no
            # annotations (form fields included): bake them into the page.
            fields = sum(1 for page in document for _ in page.widgets() or ())
            if fields:
                document.bake(annots=False, widgets=True)
                logger.info("Flattened %d form field(s) from the blank CRF into page content", fields)
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

            if fields:
                _embed_unused_field_fonts(document)
            staged = self.output_pdf.with_name(f".{self.output_pdf.name}.unoptimized")
            document.save(staged, deflate=True, garbage=3)
        finish_for_submission(staged, self.output_pdf)
        staged.unlink(missing_ok=True)

        logger.info("Annotated %s: %d boxes drawn, %d page(s) marked NOT SUBMITTED",
                    self.output_pdf.name, drawn, not_submitted)
        return self.output_pdf

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _page_palette(rows: pd.DataFrame) -> dict[str, tuple[float, float, float]]:
        """Stable colour per domain, in order of first appearance on the page."""
        palette: dict[str, tuple[float, float, float]] = {}
        for _, row in rows.iterrows():
            if str(row.get("A_TEXT") or "").strip() == NOT_SUBMITTED_TEXT:
                continue          # not a domain; must not consume a colour
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

        if text == NOT_SUBMITTED_TEXT:
            # Collected but not submitted: grey and dotted, so it reads as the
            # absence of a mapping rather than as one more domain.
            return self._add_box(page, text, (x0, y0, x1, y1),
                                 NOT_SUBMITTED_COLOR, is_header, dashes=_DOTTED)

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
        """Draw one box as page content - a filled, bordered rectangle with
        its text in the embedded annotation font. The FDA's PDF specification
        asks for no PDF annotations (comments) in submitted documents, and
        drawn text stays searchable and prints exactly as shown."""
        font_size = HEADER_FONT_SIZE if is_header else FIELD_FONT_SIZE
        x0, top, x1, bottom = rect
        # Layout gives a long annotation two or three lines in a narrow, tall box.
        if not is_header and "\n" not in text:
            if bottom - top >= THREE_LINE_HEIGHT - 0.5:
                text = "\n".join(three_lines(text))
            elif bottom - top >= TWO_LINE_HEIGHT - 0.5:
                text = "\n".join(two_lines(text))
        lines = text.count("\n") + 1
        # Edited text can be longer than the box an earlier run sized for it:
        # widen the box rather than clip the text.
        needed = max(ANNOTATION_FONT.text_length(part, fontsize=font_size) for part in text.split("\n")) + 6
        x1 = max(x1, x0 + needed)
        # Centre the text band inside the reserved rectangle.
        text_height = font_size * _LINE_HEIGHT * lines
        offset = max(0.0, (bottom - top - text_height) / 2)
        box = pymupdf.Rect(x0, top + offset, x1, bottom - offset)
        if box.is_empty or box.is_infinite:
            return False

        try:
            shape = page.new_shape()
            shape.draw_rect(box)
            shape.finish(color=BORDER_COLOR, fill=colour, width=_BORDER_WIDTH,
                         dashes="[2 2] 0" if dashes else None)
            shape.commit()
            _use_font(page)
            inner = pymupdf.Rect(box.x0 + 1, box.y0 + 0.5, box.x1 - 1, box.y1 + 2)
            spare = page.insert_textbox(inner, text, fontname=_FONT_NAME, fontsize=font_size,
                                        color=FONT_COLOR, align=pymupdf.TEXT_ALIGN_CENTER)
            if spare < 0:  # did not fit: give it the whole reserved rectangle
                page.insert_textbox(pymupdf.Rect(x0, top, x1 + 4, bottom + 4), text, fontname=_FONT_NAME,
                                    fontsize=font_size, color=FONT_COLOR, align=pymupdf.TEXT_ALIGN_CENTER)
        except Exception as exc:  # noqa: BLE001 - one bad box must not kill the run
            logger.warning("Could not draw annotation %r at %s: %s", text, rect, exc)
            return False
        return True


_FONT_NAME = "acrf"
_LINE_HEIGHT = 1.36


def _use_font(page: pymupdf.Page) -> None:
    """Make the annotation font available on a page (embedded once per file)."""
    if _FONT_NAME not in {font[4] for font in page.get_fonts()}:
        page.insert_font(fontname=_FONT_NAME, fontbuffer=ANNOTATION_FONT.buffer)


_STANDARD = ("ZapfDingbats", "Symbol")


def _embed_unused_field_fonts(document: pymupdf.Document) -> None:
    """Baked form fields keep their font resource (usually non-embedded
    Helvetica) even when no character is drawn in it - an empty field. Point
    such a resource at the embedded annotation font, so every font the PDF
    declares is embedded. A font that does draw text is left alone (and the
    FDA check reports it)."""
    replaced = 0
    for page in document:
        unembedded = [f for f in page.get_fonts(full=True) if f[1] == "n/a" and not f[3].startswith(_STANDARD)]
        if not unembedded:
            continue
        drawn = {span["font"] for span in page.get_texttrace()}
        _use_font(page)
        ours = next(f[0] for f in page.get_fonts(full=True) if f[4] == _FONT_NAME)
        for _xref, _ext, _type, basefont, name, _encoding, referencer, *_ in unembedded:
            if basefont in drawn or not referencer:
                continue
            document.xref_set_key(referencer, f"Resources/Font/{name}", f"{ours} 0 R")
            replaced += 1
    if replaced:
        logger.info("Pointed %d unused form-field font reference(s) at the embedded annotation font", replaced)


def finish_for_submission(source: Path, target: Path) -> None:
    """Open on the bookmarks panel and optimise for Fast Web View, as the FDA
    PDF specification asks ("Initial View Settings", "Optimize for Fast Web
    View")."""
    try:
        import pikepdf
    except ImportError:  # pragma: no cover - depends on install
        logger.warning("pikepdf is not installed: the aCRF is not linearized for Fast Web View")
        Path(target).write_bytes(Path(source).read_bytes())
        return
    with pikepdf.open(source) as pdf:
        if "/Outlines" in pdf.Root:
            pdf.Root.PageMode = pikepdf.Name.UseOutlines
        pdf.save(target, linearize=True)
