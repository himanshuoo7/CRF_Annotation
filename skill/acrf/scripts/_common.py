"""Shared helpers for the aCRF plugin scripts."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NoReturn

# Allow running against a source checkout instead of an installed package.
_extra = os.environ.get("ACRF_PACKAGE_PATH", "").strip()
if _extra and _extra not in sys.path:
    sys.path.insert(0, _extra)

COORD_SHEET = "coordinates"
DISPLAY = {
    "PAGE_NO(REF)": "PAGE_NO",
    "SOURCE_DATASET(REF)": "DS_OID",
    "CRF_TEXT(REF)": "ITM_TEXT",
    "FORM_NAME(REF)": "DS_NAME",
    "LEFT_BOTTOM_RIGHT_TOP": "POS1",
    "ANNOTATION": "A_TEXT",
    "ANNOTATION_TYPE_HEADER_OR_FIELD(REF)": "ANNOTATION_TYPE",
    "MATCH_METHOD(REF)": "MATCH_METHOD",
    "MATCH_CONFIDENCE(REF)": "MATCH_CONFIDENCE",
    "MATCH_NOTE(REF)": "MATCH_NOTE",
}
INTERNAL = {v: k for k, v in DISPLAY.items()}


def die(message: str, code: int = 2) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def require_pandas():
    try:
        import pandas as pd
    except ImportError:
        die("pandas is not available to this interpreter. Set the plugin's "
            "'Python interpreter' option to one with crf-annotation installed.")
    return pd


def find_workbook(target: str) -> Path:
    """Accept a workbook path or a directory containing aCRF_Config.xlsx."""
    path = Path(target).expanduser()
    if path.is_dir():
        candidate = path / "aCRF_Config.xlsx"
        if not candidate.is_file():
            die(f"no aCRF_Config.xlsx in {path}")
        return candidate
    if not path.is_file():
        die(f"not found: {path}")
    return path


def read_coordinates(workbook: Path):
    pd = require_pandas()
    try:
        frame = pd.read_excel(workbook, sheet_name=COORD_SHEET, engine="openpyxl")
    except ValueError as exc:
        if COORD_SHEET in str(exc) or "Worksheet" in str(exc):
            die(f"{workbook.name} has no '{COORD_SHEET}' sheet — is it an aCRF config workbook?")
        die(f"cannot read {workbook.name}: {exc}")
    except Exception as exc:
        # A .json or .pdf handed to us surfaces as BadZipFile from openpyxl.
        die(f"{workbook.name} is not a readable .xlsx workbook ({type(exc).__name__})")
    frame = frame.rename(columns=DISPLAY)
    for column in ("MATCH_METHOD", "MATCH_NOTE"):
        if column not in frame.columns:
            frame[column] = ""
    if "MATCH_CONFIDENCE" not in frame.columns:
        frame["MATCH_CONFIDENCE"] = 0.0
    frame["MATCH_METHOD"] = frame["MATCH_METHOD"].fillna("").astype(str)
    frame["A_TEXT"] = frame["A_TEXT"].fillna("").astype(str)
    return frame


def read_other_sheets(workbook: Path) -> dict:
    pd = require_pandas()
    sheets = {}
    with pd.ExcelFile(workbook, engine="openpyxl") as book:  # already validated
        for name in book.sheet_names:
            if name != COORD_SHEET:
                sheets[name] = book.parse(name)
    return sheets


def write_workbook(workbook: Path, coordinates, others: dict) -> None:
    pd = require_pandas()
    display = coordinates.rename(columns=INTERNAL)
    staged = workbook.with_name(f".{workbook.name}.staging")
    with pd.ExcelWriter(staged, engine="xlsxwriter") as writer:
        display.to_excel(writer, sheet_name=COORD_SHEET, index=False)
        for name, frame in others.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    os.replace(staged, workbook)


def emit(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
