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

# FDA Study Data Technical Conformance Guide §4.1.4.6: "the CRF should be
# annotated with the text 'NOT SUBMITTED'". Exactly that text, unbracketed.
NOT_SUBMITTED_TEXT: Final = "NOT SUBMITTED"
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

# Match method recorded for a field that is collected but deliberately not
# submitted. Not a match, so it is not part of the review burden.
METHOD_NOT_SUBMITTED: Final = "not_submitted"

# --- Output naming ---------------------------------------------------------
# §4.1.4.6: "The aCRF should be provided as a PDF with the file name
# 'acrf.pdf.'" Lower case, exactly — it is the submitted filename.
OUTPUT_PDF_NAME: Final = "acrf.pdf"
OUTPUT_CONFIG_NAME: Final = "aCRF_Config.xlsx"
LOG_FILE_NAME: Final = "aCRF_logs.log"

# Written into the PDF /Author field. Callers should pass their own; this is
# only what appears when they do not.
DEFAULT_PDF_AUTHOR: Final = "crf-annotation"
PDF_KEYWORDS: Final = "aCRF"

# --- SDTM variables that are never annotated on a CRF ---------------------
# These are derived or assigned during dataset creation, not collected on the
# form, so CDISC conventions say they are documented in define.xml with an
# origin of Derived or Assigned rather than annotated. Annotating one is a
# routine QC finding.
#
# A `--` prefix is the SDTM wildcard for the two-character domain code, so
# `--SEQ` covers AESEQ, VSSEQ, LBSEQ and so on.
DERIVED_VARIABLES: Final = frozenset(
    {
        "STUDYID", "USUBJID", "SUBJID", "SITEID", "DOMAIN", "RFSTDTC",
        "RFENDTC", "ARMNRS", "ARMCD", "ACTARMCD", "EPOCH",
        "--SEQ", "--STRESC", "--STRESN", "--STRESU",
        "--DY", "--STDY", "--ENDY",
    }
)

_DERIVED_EXACT: Final = frozenset(v for v in DERIVED_VARIABLES if not v.startswith("--"))
_DERIVED_SUFFIXES: Final = tuple(v[2:] for v in DERIVED_VARIABLES if v.startswith("--"))


def is_derived_variable(name: object) -> bool:
    """True when an SDTM variable is derived or assigned rather than collected.

    Matches exact names (`USUBJID`) and the `--` domain-prefix convention,
    where `--SEQ` means any two-character domain code followed by `SEQ`.
    """
    variable = str(name or "").strip().upper()
    if not variable:
        return False
    if variable in _DERIVED_EXACT:
        return True
    return any(
        variable.endswith(suffix) and len(variable) == len(suffix) + 2
        for suffix in _DERIVED_SUFFIXES
    )
