"""Placement of annotation boxes on a page.

Each annotation is placed in the first free slot to the right of its field,
then on successive rows below. Candidates are always measured from the source
text box and bounds-checked against the page, so an annotation is never
written off the edge; one that cannot be placed is counted and logged instead.

Coordinates are PDF user space (origin bottom-left).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd
import pymupdf

from .constants import (
    EMPTY_POS,
    FIELD_CHAR_WIDTH,
    FIELD_FONT_SIZE,
    FIELD_GAP_X,
    FIELD_ROW_HEIGHT,
    HEADER_TOP_MARGIN,
)
from .logging_utils import get_logger
from .pdf_text import PageGeometry, format_pos

logger = get_logger()

Rect = tuple[float, float, float, float]

_BAND = 24.0          # vertical bucket size for the collision index
_MAX_DROPS = 6        # how many rows down we try before giving up
_MIN_LEFT = 20.0      # never place an annotation in the binding margin
_MIN_TOP = 4.0        # nor off the top edge of the page
_TEXT_PADDING = 8.0   # the box border and its inner margin
_BLOCKER_GAP = 4.0    # clearance after the text a box steps past
_FINE_STEP = 5.0      # vertical search step
_COLUMN_MARGIN = 6.0  # clearance kept before the next column's text
_COLUMN_GAP = 80.0    # text starts closer than this are indents, not columns
_COLUMN_LINES = 3     # lines that must start at an x for it to count as a column


def column_edges(page_items: pd.DataFrame, min_lines: int = _COLUMN_LINES) -> list[float]:
    """Where a page's text columns start, from where its lines start.

    A multi-column CRF lays questions out in columns; a box placed across a
    column edge reads as an annotation of the neighbouring question. Starts
    used by a few lines are column candidates; ones within `_COLUMN_GAP` of a
    column to their left are indents (answer options), not new columns.
    """
    starts = pd.to_numeric(page_items.get("X0", pd.Series(dtype=float)), errors="coerce").dropna()
    if starts.empty:
        return []
    counts = (starts // 4 * 4).value_counts()
    edges: list[float] = []
    for x in sorted(counts[counts >= min_lines].index):
        if not edges or x - edges[-1] >= _COLUMN_GAP:
            edges.append(float(x))
    return edges


def column_of(x0: float, edges: list[float], right_limit: float) -> tuple[float, float]:
    """The (left, right) bounds of the column a field starting at `x0` sits in."""
    left = max((e for e in edges if e <= x0 + 4), default=_MIN_LEFT)
    right = min((e - _COLUMN_MARGIN for e in edges if e > x0 + 4), default=right_limit)
    return max(left, _MIN_LEFT), min(right, right_limit)


TWO_LINE_HEIGHT = FIELD_ROW_HEIGHT * 1.8
THREE_LINE_HEIGHT = FIELD_ROW_HEIGHT * 2.55


def three_lines(text: str) -> list[str]:
    """`LB.LBORRES` / `where LBTESTCD` / `= AST`: the narrowest box, for a
    question whose only free space before the next one is short."""
    head, *rest = two_lines(text)
    if not rest:
        return [head]
    tail = rest[0]
    if " = " in tail:
        left, right = tail.rsplit(" = ", 1)
        return [head, left, f"= {right}"]
    second = two_lines(tail)
    return [head, *second] if len(second) == 2 else [head, tail]


def two_lines(text: str) -> list[str]:
    """A long annotation split for a narrow column: `IE.IEORRES` /
    `where IETESTCD = INCL02`, else at the space nearest the middle."""
    text = str(text)
    if " where " in text:
        head, tail = text.split(" where ", 1)
        return [head, f"where {tail}"]
    spaces = [i for i, c in enumerate(text) if c == " "]
    if not spaces:
        return [text]
    cut = min(spaces, key=lambda i: abs(i - len(text) / 2))
    return [text[:cut], text[cut + 1:]]


def _annotation_font() -> pymupdf.Font:
    """Noto Sans (SIL OFL), embedded in full in every aCRF: the FDA asks for
    non-standard fonts to be fully embedded, and a font the reviewer's
    machine may lack is never substituted."""
    try:
        return pymupdf.Font("notos")          # from the pymupdf-fonts package
    except Exception:  # noqa: BLE001 - without it, fall back (the FDA check will say so)
        return pymupdf.Font("helv")


ANNOTATION_FONT = _annotation_font()


def annotation_width(text: str) -> float:
    """Box width for an annotation, measured in the font it is drawn in.

    A per-character estimate overshoots by 10-20%, which is the difference
    between a box fitting beside a short "Yes"/"No" in a narrow column and
    being pushed to the foot of the column, away from its question.
    """
    try:
        measured = ANNOTATION_FONT.text_length(str(text), fontsize=FIELD_FONT_SIZE)
    except Exception:  # noqa: BLE001 - fall back to the estimate
        measured = len(str(text)) * FIELD_CHAR_WIDTH
    return measured + _TEXT_PADDING


class _Occupancy:
    """Band-bucketed rectangle index: overlap tests stay local to the row."""

    def __init__(self, rects: Iterable[Rect] = ()) -> None:
        self._bands: dict[int, list[Rect]] = {}
        for rect in rects:
            self.add(rect)

    @staticmethod
    def _band_range(y0: float, y1: float) -> range:
        return range(int(y0 // _BAND), int(y1 // _BAND) + 1)

    def add(self, rect: Rect) -> None:
        for band in self._band_range(rect[1], rect[3]):
            self._bands.setdefault(band, []).append(rect)

    def collides(self, rect: Rect) -> bool:
        return self.blocker(rect) is not None

    def blocker(self, rect: Rect) -> Rect | None:
        """The rectangle in the way that reaches furthest right, if any."""
        x0, y0, x1, y1 = rect
        found: Rect | None = None
        for band in self._band_range(y0, y1):
            for other in self._bands.get(band, ()):
                overlaps = x0 < other[2] and other[0] < x1 and y0 < other[3] and other[1] < y1
                if overlaps and (found is None or other[2] > found[2]):
                    found = other
        return found


@dataclass(frozen=True)
class PlacementStats:
    placed: int = 0
    dropped: int = 0


def place_annotations(merged: pd.DataFrame, geometry: PageGeometry) -> tuple[pd.DataFrame, PlacementStats]:
    """Assign every annotated row a non-overlapping `POS1` rectangle.

    Rows without annotation text keep `[0,0,0,0]`, which downstream code reads
    as "nothing to draw".
    """
    result = merged.copy()
    if result.empty:
        return result.assign(POS1=pd.Series(dtype=object)), PlacementStats()

    result["POS1"] = EMPTY_POS
    annotated = result["ATEXT"].fillna("").astype(str).str.strip() != ""
    if not annotated.any():
        logger.warning("No annotations to place")
        return result, PlacementStats()

    placed = dropped = 0
    positions: dict[int, str] = {}

    for page_no, page_rows in result[annotated].groupby("PAGE_NO", sort=True):
        width = geometry.width(int(page_no))
        height = geometry.height(int(page_no))
        # Every piece of printed text on the page is an obstacle, plus the band
        # reserved at the top for domain headers.
        page_items = result[result["PAGE_NO"] == page_no]
        header_band = (0.0, height - HEADER_TOP_MARGIN - 3 * FIELD_ROW_HEIGHT, width, height)
        occupancy = _Occupancy([*_item_rects(page_items), header_band])
        # Entry-box glyphs, underscores and other wordless marks: a box may sit
        # over them when that is the only way to stay beside its own question.
        wordy = page_items[page_items["ITM_TEXT"].map(_has_words)] if "ITM_TEXT" in page_items.columns else page_items
        soft = _Occupancy([*_item_rects(wordy), header_band])
        # Columns from where the page's *questions* start: date masks and
        # answer options start at odd places and would fake extra columns.
        labels = page_items[page_items["VAR_OID"].notna()] if "VAR_OID" in page_items.columns else page_items
        edges = column_edges(labels, min_lines=2) if len(labels) >= 4 else column_edges(page_items)
        right_limit = width - _MIN_LEFT

        # Top-to-bottom, left-to-right: stable output and better packing.
        ordered = page_rows.sort_values(["Y0", "X0"], ascending=[False, True], kind="stable")
        for index, row in ordered.iterrows():
            floor = _next_question_top(row, labels, edges, right_limit)
            rect = _place_one(row, occupancy, width, height, edges, floor=floor, soft=soft)
            if rect is None:
                dropped += 1
                continue
            occupancy.add(rect)
            soft.add(rect)
            positions[index] = format_pos(*rect)
            placed += 1

    if positions:
        # Positional assignment (not a Series) so this is safe even if the
        # caller handed us a frame with a non-unique index.
        result.loc[list(positions.keys()), "POS1"] = list(positions.values())

    if dropped:
        logger.warning("%d annotation(s) had no free space on their page and were skipped", dropped)
    logger.info("Placed %d annotation boxes", placed)
    return result, PlacementStats(placed=placed, dropped=dropped)


def place_box(anchor: pd.Series, text: str, page_items: pd.DataFrame, boxes: Iterable[Rect],
              page_width: float, page_height: float) -> Rect | None:
    """Place one annotation beside `anchor` (a printed line) on a page that
    already carries `boxes`: the same rules as a full run, for an edit."""
    # No band is held back for domain banners here: after an edit they are
    # re-laid out around the page's text and boxes, this one included.
    boxes = list(boxes)
    occupancy = _Occupancy([*_item_rects(page_items), *boxes])
    wordy = page_items[page_items["ITM_TEXT"].map(_has_words)]
    soft = _Occupancy([*_item_rects(wordy), *boxes])
    row = pd.Series({"X0": anchor["X0"], "X1": anchor["X1"], "Y0": anchor["Y0"], "Y1": anchor["Y1"], "ATEXT": text})
    # an edit names one line: stay within two rows of it, else just above it
    floor = float(anchor["Y0"]) - 2 * FIELD_ROW_HEIGHT
    return _place_one(row, occupancy, page_width, page_height, column_edges(page_items), floor=floor,
                      soft=soft, above_first=True)


def _has_words(text: object) -> bool:
    return any(ch.isalnum() for ch in str(text or ""))


def _next_question_top(row: pd.Series, labels: pd.DataFrame, edges: list[float], right_limit: float) -> float | None:
    """Top edge of the next question below this field in its column.

    A box below that line sits beside the next question and reads as its
    annotation, so the search near a field stops there.
    """
    try:
        x0, y0 = float(row["X0"]), float(row["Y0"])
    except (TypeError, ValueError):
        return None
    if labels.empty or "VAR_OID" not in labels.columns:
        return None
    column = column_of(x0, edges, right_limit)
    others = labels[(labels["VAR_OID"] != row.get("VAR_OID")) & (pd.to_numeric(labels["Y1"], errors="coerce") < y0)]
    below = [float(r["Y1"]) for _, r in others.iterrows() if column_of(float(r["X0"]), edges, right_limit) == column]
    return max(below) if below else None


def _item_rects(rows: pd.DataFrame) -> list[Rect]:
    frame = rows[["X0", "Y0", "X1", "Y1"]].apply(pd.to_numeric, errors="coerce").dropna()
    return [tuple(values) for values in frame.drop_duplicates().to_numpy()]


def _place_one(row: pd.Series, occupancy: _Occupancy, page_width: float, page_height: float,
               edges: list[float] | None = None, floor: float | None = None,
               soft: _Occupancy | None = None, above_first: bool = False) -> Rect | None:
    """A free slot in the field's own column: beside it, then the rows below
    it, on one line or wrapped onto two, then further down the column, then
    above it. Never in another column: a box there reads as the neighbouring
    question's annotation, which is worse than a box reported as unplaced."""
    try:
        x0 = float(row["X0"])
        x1 = float(row["X1"])
        y0 = float(row["Y0"])
        y1 = float(row["Y1"])
    except (TypeError, ValueError):
        return None

    text = str(row["ATEXT"])
    right_limit = page_width - _MIN_LEFT
    col_left, col_right = column_of(x0, edges or [], right_limit)
    if x1 > col_right:
        # a title or heading that runs across columns: the free space beside
        # it is to the right of where it ends, up to the page edge
        col_right = right_limit
    line_height = max(FIELD_ROW_HEIGHT, y1 - y0)
    shapes = [(annotation_width(text), line_height)]
    lines = two_lines(text)
    if len(lines) == 2:
        shapes.append((max(annotation_width(line) for line in lines), max(TWO_LINE_HEIGHT, y1 - y0)))
    lines = three_lines(text)
    if len(lines) == 3:
        shapes.append((max(annotation_width(line) for line in lines), max(THREE_LINE_HEIGHT, y1 - y0)))
    fitting = [shape for shape in shapes if shape[0] <= col_right - col_left]
    if not fitting:
        # wider than its column even wrapped: let the narrowest run on rightwards
        fitting, col_right = [min(shapes)], right_limit

    def scan(top: float, start: float, width: float, height: float, where: _Occupancy = occupancy) -> Rect | None:
        left = start
        while left + width <= col_right:
            candidate = (left, top, left + width, top + height)
            blocker = where.blocker(candidate)
            if blocker is None:
                return candidate
            # step just past what is in the way: a narrow column has room
            # right after a short "Yes"/"No", which a coarse step jumps over
            left = max(left + 1.0, blocker[2] + _BLOCKER_GAP)
        return None

    near = _MAX_DROPS * FIELD_ROW_HEIGHT
    # 1. beside the field, then just under it - in small steps, so the gap
    # beside a question's own Yes/No rows is found before the next question's
    # (both shapes at each height: the nearest slot wins, one line preferred)
    # Never below the next question's label: first around printed words
    # only, then over wordless marks (entry boxes) if that is what it takes.
    lowest = max(0.0, y0 - near)
    for where in (occupancy, soft) if soft is not None else (occupancy,):
        for top in _steps(y0, lowest, -_FINE_STEP):
            for width, height in fitting:
                if floor is not None and top < floor + 1.0:
                    continue
                start = x1 + FIELD_GAP_X if top == y0 else max(col_left, min(x0, col_right - width))
                found = scan(top, start, width, height, where)
                if found:
                    return found
        if floor is None:
            break
    if above_first:
        # just above the line (a title or a heading has room over it more
        # often than beside it), before drifting further down the page
        ceiling = page_height - _MIN_TOP
        for top in _steps(y1 + 2.0, y1 + 2.0 + 2 * FIELD_ROW_HEIGHT, _FINE_STEP):
            if top + max(h for _, h in fitting) > ceiling:
                break
            for width, height in fitting:
                found = scan(top, max(col_left, min(x0, col_right - width)), width, height)
                if found:
                    return found
    for top in _steps(y0, lowest, -_FINE_STEP):  # no room above the next question
        for width, height in fitting:
            start = x1 + FIELD_GAP_X if top == y0 else max(col_left, min(x0, col_right - width))
            found = scan(top, start, width, height)
            if found:
                return found
    # 2. further down its own column, then 3. above it, in the narrowest shape
    width, height = min(fitting)
    row_start = max(col_left, min(x0, col_right - width))
    for top in (*_steps(y0 - near, 0.0, -_FINE_STEP),
                *_steps(y1 + 2.0, page_height - HEADER_TOP_MARGIN - 3 * FIELD_ROW_HEIGHT - height, _FINE_STEP)):
        found = scan(top, row_start, width, height)
        if found:
            return found
    return None


def _steps(start: float, stop: float, step: float) -> list[float]:
    values, value = [], start
    while (step < 0 and value >= stop) or (step > 0 and value <= stop):
        values.append(value)
        value += step
    return values
