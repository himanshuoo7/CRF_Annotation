"""Text extraction from the blank CRF.

Coordinates are returned in PDF user space (origin bottom-left), the same
convention the config workbook uses, so nothing downstream has to guess which
way up a rectangle is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pymupdf

from .constants import DEFAULT_PAGE_HEIGHT, DEFAULT_PAGE_WIDTH
from .logging_utils import get_logger

logger = get_logger()

ITEM_COLUMNS = ["PAGE_NO", "ITM_TEXT", "ITM_DIM", "X0", "Y0", "X1", "Y1"]


@dataclass(frozen=True)
class PageGeometry:
    """Width/height per 1-based page number, with sane fallbacks."""

    sizes: dict[int, tuple[float, float]]

    def width(self, page_no: int) -> float:
        return self.sizes.get(page_no, (DEFAULT_PAGE_WIDTH, DEFAULT_PAGE_HEIGHT))[0]

    def height(self, page_no: int) -> float:
        return self.sizes.get(page_no, (DEFAULT_PAGE_WIDTH, DEFAULT_PAGE_HEIGHT))[1]

    @property
    def page_count(self) -> int:
        return len(self.sizes)


def format_pos(x0: float, y0: float, x1: float, y1: float) -> str:
    """Serialise a rectangle the way the config workbook stores it."""
    return f"[{round(x0, 2)}, {round(y0, 2)}, {round(x1, 2)}, {round(y1, 2)}]"


def extract_items(pdf_path: Path) -> tuple[pd.DataFrame, PageGeometry]:
    """Read every text line of the CRF into a frame of annotatable items."""
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"Input PDF not found: {path}")

    records: list[dict[str, object]] = []
    sizes: dict[int, tuple[float, float]] = {}

    with pymupdf.open(path) as document:
        for index, page in enumerate(document, start=1):
            rect = page.rect
            page_height = float(rect.height)
            sizes[index] = (float(rect.width), page_height)

            for block in page.get_text("dict", sort=True).get("blocks", ()):
                for line in block.get("lines", ()):
                    spans = line.get("spans") or ()
                    if not spans:
                        continue
                    # Whitespace is never significant: every downstream
                    # comparison normalises it, and the page/form join is on a
                    # resolved form OID rather than on printed text.
                    text = "".join(span.get("text", "") for span in spans).strip()
                    if not text:
                        continue

                    x0 = min(span["bbox"][0] for span in spans)
                    top = min(span["bbox"][1] for span in spans)
                    x1 = max(span["bbox"][2] for span in spans)
                    bottom = max(span["bbox"][3] for span in spans)
                    # Flip to a bottom-left origin.
                    y0, y1 = page_height - bottom, page_height - top

                    records.append(
                        {
                            "PAGE_NO": index,
                            "ITM_TEXT": text,
                            "ITM_DIM": format_pos(x0, y0, x1, y1),
                            "X0": round(x0, 2),
                            "Y0": round(y0, 2),
                            "X1": round(x1, 2),
                            "Y1": round(y1, 2),
                        }
                    )

    items = pd.DataFrame(records, columns=ITEM_COLUMNS)
    logger.info("Extracted %d text items from %d pages of %s", len(items), len(sizes), path.name)
    return items, PageGeometry(sizes)
