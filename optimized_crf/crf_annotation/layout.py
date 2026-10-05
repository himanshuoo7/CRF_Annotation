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

from .constants import (
    EMPTY_POS,
    FIELD_CHAR_WIDTH,
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
_TEXT_PADDING = 6.0


def annotation_width(text: str) -> float:
    return len(str(text)) * FIELD_CHAR_WIDTH + _TEXT_PADDING


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
        x0, y0, x1, y1 = rect
        for band in self._band_range(y0, y1):
            for other in self._bands.get(band, ()):
                if x0 < other[2] and other[0] < x1 and y0 < other[3] and other[1] < y1:
                    return True
        return False


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
        occupancy = _Occupancy(_item_rects(page_items))
        occupancy.add((0.0, height - HEADER_TOP_MARGIN - 3 * FIELD_ROW_HEIGHT, width, height))

        # Top-to-bottom, left-to-right: stable output and better packing.
        ordered = page_rows.sort_values(["Y0", "X0"], ascending=[False, True], kind="stable")
        for index, row in ordered.iterrows():
            rect = _place_one(row, occupancy, width, height)
            if rect is None:
                dropped += 1
                continue
            occupancy.add(rect)
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


_NEAR_REACH = 60.0   # how far a box may slide sideways before dropping a row


def _item_rects(rows: pd.DataFrame) -> list[Rect]:
    frame = rows[["X0", "Y0", "X1", "Y1"]].apply(pd.to_numeric, errors="coerce").dropna()
    return [tuple(values) for values in frame.drop_duplicates().to_numpy()]


def _place_one(row: pd.Series, occupancy: _Occupancy, page_width: float, page_height: float) -> Rect | None:
    """First free slot: to the right of the field, then rows below it in the
    field's own column, and only then rows below from the left margin."""
    try:
        x0 = float(row["X0"])
        x1 = float(row["X1"])
        y0 = float(row["Y0"])
        y1 = float(row["Y1"])
    except (TypeError, ValueError):
        return None

    box_width = annotation_width(row["ATEXT"])
    box_height = max(FIELD_ROW_HEIGHT, y1 - y0)
    right_limit = page_width - _MIN_LEFT

    # A question in the right-hand column has no room beside it; its box must
    # still land in that column, under it, not across the page beside another
    # field where a reviewer would read it as that field's annotation.
    # A wide box (a SUPP-- QNAM) in a narrow right column starts further left
    # so it still fits under its own question.
    own_column = max(_MIN_LEFT, min(x0, right_limit - box_width))
    for column_start, reach in ((own_column, _NEAR_REACH), (_MIN_LEFT, page_width)):
        for drop in range(_MAX_DROPS):
            top = y0 - drop * (box_height + 2.0)
            if top < 0:
                break
            left = x1 + FIELD_GAP_X if drop == 0 else column_start
            # Beside the field means near it: sliding across into the next
            # column would put the box beside a different field. Rows below
            # start in the field's own column and may run on past its options.
            limit = min(right_limit, left + box_width + reach) if drop == 0 else right_limit
            while left + box_width <= limit:
                candidate = (left, top, left + box_width, top + box_height)
                if not occupancy.collides(candidate):
                    return candidate
                left += box_width / 2 + FIELD_GAP_X
    return None
