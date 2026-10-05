"""Follow-up chat on a finished aCRF: questions, and edits that re-render it.

Edits go through the config workbook, the same reviewable contract a human
would edit by hand: the model only proposes new annotation text for rows it
names, each change is checked, the row is marked "reviewed", and the PDF is
re-rendered from the workbook (a rerun). Without a model the chat still
answers the most useful question — what needs review — deterministically.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from crf_annotation import annotate_crf
from crf_annotation.config_workbook import read_config_workbook, write_config_workbook
from crf_annotation.constants import FILENAMES_SHEET, METHOD_NOT_SUBMITTED, UNMAPPED_SHEET
from crf_annotation.job import CrfAnnotationError

_REVIEW_WORDS = re.compile(r"\b(review|check|unsure|uncertain|confidence|flag|verify)\b", re.IGNORECASE)
_ANNOTATION = re.compile(r"^[A-Z][A-Z0-9]{1,7}(\.[A-Z][A-Z0-9]{0,7})?([ A-Za-z0-9=._'\"-]{0,160})$|^\[?NOT SUBMITTED\]?$")
_SETTLED = {"", "exact", "reviewed", METHOD_NOT_SUBMITTED}

_SYSTEM = (
    "You are the annotated-CRF assistant for a clinical programmer. You see the annotations on "
    "their SDTM aCRF: each row has an id, page, the CRF text and its annotation (SDTM variable, "
    "e.g. AE.AETERM or VS.VSORRES where VSTESTCD = SYSBP), plus how it was matched. Answer "
    "questions briefly and concretely, citing pages. When the user asks to change annotations, "
    "return edits naming the row ids and the full new annotation text; never edit rows they did "
    "not ask about. If a request is ambiguous, ask instead of editing."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"row": {"type": "integer"}, "annotation": {"type": "string"}},
                "required": ["row", "annotation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reply", "edits"],
    "additionalProperties": False,
}


def _rows(coordinates: pd.DataFrame) -> pd.DataFrame:
    if "ANNOTATION_TYPE" in coordinates.columns:
        return coordinates[coordinates["ANNOTATION_TYPE"].astype(str) == "Field"]
    return coordinates


def _needs_review(fields: pd.DataFrame) -> pd.DataFrame:
    methods = fields.get("MATCH_METHOD", pd.Series("", index=fields.index)).fillna("").astype(str)
    return fields[~methods.isin(_SETTLED)]


def _review_reply(fields: pd.DataFrame) -> str:
    pending = _needs_review(fields)
    if pending.empty:
        return "Nothing is flagged for review — every annotation came from an exact match or has been reviewed."
    lines = [
        f"• Page {int(row.PAGE_NO)}: “{str(row.ITM_TEXT)[:60]}” → {row.A_TEXT} ({row.MATCH_METHOD})"
        for row in pending.head(15).itertuples()
    ]
    more = f"\n…and {len(pending) - 15} more in the review workbook." if len(pending) > 15 else ""
    return f"{len(pending)} annotation(s) need a look:\n" + "\n".join(lines) + more


def _client():
    from crf_annotation.ai import AiSettings
    from crf_annotation.ai.client import ClaudeClient

    client = ClaudeClient(AiSettings.from_env())
    return client if client.available() else None


def answer(root: Path, message: str, study: str = "Study", client: Any = "auto") -> dict[str, Any]:
    workbook = root / "out" / "aCRF_Config.xlsx"
    if not workbook.is_file():
        return {"reply": "Run the annotation first — then ask me anything about the result.", "edits_applied": 0}
    coordinates = read_config_workbook(workbook)
    fields = _rows(coordinates)

    model = _client() if client == "auto" else client
    if model is None:
        if _REVIEW_WORDS.search(message):
            return {"reply": _review_reply(fields), "edits_applied": 0}
        return {"reply": "I can list what needs review right now. Plain-English edits need the AI "
                         "assistant, which isn't switched on for this workspace.", "edits_applied": 0}

    table = [
        {"id": int(index), "page": int(row["PAGE_NO"]), "crf_text": str(row["ITM_TEXT"])[:80],
         "annotation": str(row["A_TEXT"]), "matched": str(row.get("MATCH_METHOD", ""))}
        for index, row in fields.iterrows()
    ]
    response = model.json_call(
        system=_SYSTEM,
        user=f"Study {study}. Annotations:\n{json.dumps(table)}\n\nUser: {message}",
        schema=_SCHEMA,
        cache_salt=None,
    ) or {}
    reply = str(response.get("reply") or "Sorry — I couldn't work that out. Try rephrasing?")

    applied = 0
    for edit in response.get("edits") or ():
        row, text = edit.get("row"), str(edit.get("annotation") or "").strip()
        if row in fields.index and _ANNOTATION.match(text):
            coordinates.at[row, "A_TEXT"] = text
            coordinates.at[row, "MATCH_METHOD"] = "reviewed"
            coordinates.at[row, "MATCH_NOTE"] = "edited in chat"
            applied += 1
    if not applied:
        return {"reply": reply, "edits_applied": 0}

    sheets = pd.read_excel(workbook, sheet_name=None, engine="openpyxl")
    write_config_workbook(workbook, coordinates, sheets.get(UNMAPPED_SHEET), sheets.get(FILENAMES_SHEET))
    try:
        result = annotate_crf(
            crf=root / "inputs" / "crf", config=workbook, output_dir=root / "out",
            design=root / "inputs" / "design", study=study, author="Vajrra aCRF",
        )
    except CrfAnnotationError as exc:
        return {"reply": f"{reply}\n\nI couldn't re-render the CRF: {exc}", "edits_applied": applied}
    data = result.to_dict()
    data.pop("acrf", None)
    data.pop("config", None)
    return {"reply": reply, "edits_applied": applied, "result": data}
