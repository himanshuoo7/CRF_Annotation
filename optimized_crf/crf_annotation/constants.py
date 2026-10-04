"""Shared constants: geometry, styling and the config-workbook column contract.

Magic numbers and repeated literals live here exactly once, so geometry and
the workbook contract have a single definition.
"""

from __future__ import annotations

from typing import Final

# --- Page geometry ---------------------------------------------------------
# Fallbacks only. Real page sizes are read from each page (`page.rect`) —
# assuming US Letter silently corrupts coordinates on A4 and landscape CRFs.
DEFAULT_PAGE_WIDTH: Final = 612.0
DEFAULT_PAGE_HEIGHT: Final = 792.0

# --- Annotation layout -----------------------------------------------------
HEADER_FONT_SIZE: Final = 10.0
FIELD_FONT_SIZE: Final = 9.0
HEADER_CHAR_WIDTH: Final = 6.0
FIELD_CHAR_WIDTH: Final = 5.8
HEADER_ROW_HEIGHT: Final = 15.0
FIELD_ROW_HEIGHT: Final = 15.0
HEADER_LEFT_MARGIN: Final = 60.0
HEADER_TOP_MARGIN: Final = 30.0
HEADER_RIGHT_MARGIN: Final = 30.0
HEADER_GAP_X: Final = 2.0
HEADER_GAP_Y: Final = 3.0
FIELD_GAP_X: Final = 10.0

NOT_SUBMITTED_TEXT: Final = "[NOT SUBMITTED]"
FORM_PREFIX: Final = "Form: "
EMPTY_POS: Final = "[0,0,0,0]"

# --- Colours (RGB 0..1) ----------------------------------------------------
FONT_COLOR: Final = (0.0, 0.0, 0.0)
BORDER_COLOR: Final = (0.0, 0.0, 0.0)
NOT_SUBMITTED_COLOR: Final = (0.9, 0.9, 0.9)
DOMAIN_COLORS: Final = (
    (0.74901961, 1.0, 1.0),
    (1.0, 1.0, 0.58823528),
    (0.58823528, 1.0, 0.58823528),
    (1.0, 0.74509804, 0.60784314),
    (0.81176471, 0.59215686, 0.88627451),
)

# --- Config workbook contract ---------------------------------------------
# One ordered, *invertible* mapping. The old ExcelColumnMapper mapped both
# A_TEXT and ATEXT onto ANNOTATION, so its reverse map silently dropped one of
# them and downstream reads picked up the wrong column.
COORDINATE_SHEET: Final = "coordinates"
UNMAPPED_SHEET: Final = "unmap"
FILENAMES_SHEET: Final = "FileNames"

INTERNAL_TO_DISPLAY: Final = {
    "PAGE_NO": "PAGE_NO(REF)",
    "DS_OID": "SOURCE_DATASET(REF)",
    "ITM_TEXT": "CRF_TEXT(REF)",
    "DS_NAME": "FORM_NAME(REF)",
    "POS1": "LEFT_BOTTOM_RIGHT_TOP",
    "A_TEXT": "ANNOTATION",
    "ANNOTATION_TYPE": "ANNOTATION_TYPE_HEADER_OR_FIELD(REF)",
    # Provenance: how the CRF text was matched to an EDC field. "exact" needs
    # no review; anything else is a judgement call a human should confirm.
    "MATCH_METHOD": "MATCH_METHOD(REF)",
    "MATCH_CONFIDENCE": "MATCH_CONFIDENCE(REF)",
    "MATCH_NOTE": "MATCH_NOTE(REF)",
}
DISPLAY_TO_INTERNAL: Final = {v: k for k, v in INTERNAL_TO_DISPLAY.items()}
COORDINATE_COLUMNS: Final = tuple(INTERNAL_TO_DISPLAY)

ANNOTATION_HEADER: Final = "Header"
ANNOTATION_FIELD: Final = "Field"
# Form banners carry no annotation of their own; they are recorded so the
# workbook keeps a complete form -> page map for bookmarking, including pages
# that end up with nothing submitted.
ANNOTATION_FORM: Final = "Form"

# --- Output naming ---------------------------------------------------------
OUTPUT_PDF_NAME: Final = "aCRF.pdf"
OUTPUT_CONFIG_NAME: Final = "aCRF_Config.xlsx"
LOG_FILE_NAME: Final = "aCRF_logs.log"

# Written into the PDF /Author field. Callers should pass their own; this is
# only what appears when they do not.
DEFAULT_PDF_AUTHOR: Final = "crf-annotation"
PDF_KEYWORDS: Final = "aCRF"

# --- SDTM variables never annotated on a CRF ------------------------------
IGNORED_VARIABLES: Final = frozenset(
    {
        "STUDYID", "USUBJID", "SITEID", "DOMAIN", "ARMNRS", "ARMCD",
        "ACTARMCD", "--SEQ", "--STRESC", "--STRESN", "--DY", "--STDY",
        "--ENDY", "EPOCH",
    }
)
