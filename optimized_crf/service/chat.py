"""Follow-up chat on a finished aCRF: questions, and edits that re-render it.

Edits go through the config workbook, the same reviewable contract a human
would edit by hand. The model only proposes; this module checks each edit and
applies it:

* **change** an annotation's text,
* **remove** an annotation box,
* **add** an annotation beside a printed line that has none,
* **move** an annotation beside a different printed line.

Added and moved boxes are placed by the same layout rules as a full run, the
page's domain banners follow its annotations, every edited row is marked
"reviewed", and the PDF is re-rendered from the workbook. Each applied edit
comes back as a before/after record for the job's audit trail. Without a
model the chat still answers the most useful question - what needs review -
deterministically.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from crf_annotation import annotate_crf
from crf_annotation.ai.propose_specs import DOMAIN_LABELS
from crf_annotation.annotate import parse_pos
from crf_annotation.config_workbook import (
    header_rows_for_page,
    page_obstacles,
    read_config_workbook,
    write_config_workbook,
)
from crf_annotation.constants import (
    ANNOTATION_FIELD,
    ANNOTATION_HEADER,
    FILENAMES_SHEET,
    METHOD_NOT_SUBMITTED,
    UNMAPPED_SHEET,
)
from crf_annotation.job import CrfAnnotationError
from crf_annotation.layout import place_box
from crf_annotation.pdf_text import PageGeometry, extract_items, format_pos
from crf_annotation.specs import domain_of

_REVIEW_WORDS = re.compile(r"\b(review|check|unsure|uncertain|confidence|flag|verify)\b", re.IGNORECASE)
_ANNOTATION = re.compile(r"^[A-Z][A-Z0-9]{1,7}(\.[A-Z][A-Z0-9]{0,7})?([ A-Za-z0-9=._'\"-]{0,160})$|^\[?NOT SUBMITTED\]?$")
_SETTLED = {"", "exact", "reviewed", METHOD_NOT_SUBMITTED}
_PAGES = re.compile(r"\b(?:pages?|pg|p)\.?\s*(\d{1,4})(?:\s*(?:-|to|and|,)\s*(\d{1,4}))?", re.IGNORECASE)
_MAX_LINES = 1500

_SYSTEM = (
    "You are the annotated-CRF assistant for a clinical programmer. You see the annotations on "
    "their SDTM aCRF: each row has an id, page, the CRF text it sits beside and its annotation "
    "(SDTM variable, e.g. AE.AETERM or VS.VSORRES where VSTESTCD = SYSBP), plus how it was matched. "
    "When the user names pages, you also see the printed lines of those pages, each with a line id.\n\n"
    "Answer questions briefly and concretely, citing pages. When the user asks for changes, return "
    "edits:\n"
    "- change: a row's annotation text (row = its id, annotation = the full new text)\n"
    "- remove: delete a row's annotation box (row = its id)\n"
    "- add: a new annotation beside a printed line (line = its id, annotation = the text)\n"
    "- move: put a row's box beside a different printed line (row and line)\n"
    "Use -1 for row or line when an action does not need it. Each edit is checked before it is "
    "applied and the result is reported separately, so describe edits as what you will do (\"I'll add "
    "...\"), never as done. Only edit what the user asked for. If "
    "the request is ambiguous, or you need to see a page's lines and the user did not say which page, "
    "ask instead of editing."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["change", "remove", "add", "move"]},
                    "row": {"type": "integer"},
                    "line": {"type": "integer"},
                    "annotation": {"type": "string"},
                },
                "required": ["action", "row", "line", "annotation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reply", "edits"],
    "additionalProperties": False,
}


def _rows(coordinates: pd.DataFrame) -> pd.DataFrame:
    if "ANNOTATION_TYPE" in coordinates.columns:
        return coordinates[coordinates["ANNOTATION_TYPE"].astype(str) == ANNOTATION_FIELD]
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


def pages_named(message: str) -> set[int]:
    """'page 3', 'p. 12', 'pages 4-6', 'pages 2 and 5' -> the page numbers."""
    pages: set[int] = set()
    for first, last in _PAGES.findall(message):
        start = int(first)
        end = int(last) if last else start
        if end < start or end - start > 20:
            end = start
        pages.update(range(start, end + 1))
    return pages


def _crf_pdf(root: Path) -> Optional[Path]:
    found = sorted((root / "inputs" / "crf").glob("*.pdf"))
    return found[0] if found else None


class _Page:
    """The printed lines and page sizes of the blank CRF, read once per edit."""

    def __init__(self, root: Path) -> None:
        pdf = _crf_pdf(root)
        self.items, self.geometry = extract_items(pdf) if pdf else (pd.DataFrame(), PageGeometry({}))

    def lines(self, pages: set[int]) -> list[dict[str, Any]]:
        if self.items.empty or not pages:
            return []
        shown = self.items[self.items["PAGE_NO"].isin(pages)]
        shown = shown[shown["ITM_TEXT"].astype(str).str.contains(r"[A-Za-z0-9]", regex=True)]
        return [{"line": int(index), "page": int(row["PAGE_NO"]), "text": str(row["ITM_TEXT"])[:80]}
                for index, row in shown.head(_MAX_LINES).iterrows()]

    def line(self, line_id: int) -> Optional[pd.Series]:
        return self.items.loc[line_id] if line_id in self.items.index else None

    def place(self, coordinates: pd.DataFrame, anchor: pd.Series, text: str,
              skip_row: Optional[int] = None) -> Optional[str]:
        page = int(anchor["PAGE_NO"])
        on_page = _rows(coordinates)
        on_page = on_page[(on_page["PAGE_NO"].astype(int) == page) & (on_page.index != skip_row)]
        boxes = [rect for rect in (parse_pos(value) for value in on_page["POS1"]) if rect]
        page_items = self.items[self.items["PAGE_NO"] == page]
        rect = place_box(anchor, text, page_items, boxes,
                         self.geometry.width(page), self.geometry.height(page))
        return format_pos(*rect) if rect else None


def _relayout_headers(coordinates: pd.DataFrame, page: int, geometry: PageGeometry,
                      page_items: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Banners follow the page's annotations: one per domain still on it."""
    types = coordinates["ANNOTATION_TYPE"].astype(str)
    on_page = coordinates["PAGE_NO"].astype(int) == page
    old = coordinates[on_page & (types == ANNOTATION_HEADER)]
    labels = {str(text).split(" ", 1)[0]: str(text) for text in old["A_TEXT"]}
    fields = coordinates[on_page & (types == ANNOTATION_FIELD)]
    domains = sorted({d for d in fields["A_TEXT"].map(domain_of) if d})
    texts = [labels.get(d) or f"{d} ({DOMAIN_LABELS.get(d, d)})" for d in domains]
    template = old.iloc[0] if not old.empty else (fields.iloc[0] if not fields.empty else None)
    obstacles = page_obstacles(fields)
    if page_items is not None and not page_items.empty:
        obstacles += page_obstacles(page_items[page_items["PAGE_NO"] == page])
    rows = header_rows_for_page(
        page, texts, geometry.width(page), geometry.height(page), obstacles=obstacles,
        ds_oid="" if template is None else template.get("DS_OID", ""),
        itm_text="" if template is None else template.get("ITM_TEXT", ""),
        ds_name="" if template is None else template.get("DS_NAME", ""),
    )
    kept = coordinates.drop(index=old.index)
    if not rows:
        return kept
    start = int(kept.index.max()) + 1 if not kept.empty else 0
    return pd.concat([kept, pd.DataFrame(rows, index=range(start, start + len(rows)))])


def apply_edits(root: Path, coordinates: pd.DataFrame, edits: list[dict[str, Any]],
                page: Optional[_Page] = None) -> tuple[pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]]]:
    """Check and apply edits. Returns the workbook, one record per applied edit,
    and one per refused edit with the reason (both go to the audit trail)."""
    coordinates = coordinates.copy()
    applied: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    touched: set[int] = set()

    def refuse(edit: dict[str, Any], reason: str) -> None:
        refused.append({"action": "edit_refused", "requested": str(edit.get("action") or ""),
                        "annotation": str(edit.get("annotation") or ""), "reason": reason})

    def reviewed(index: int, note: str) -> None:
        coordinates.at[index, "MATCH_METHOD"] = "reviewed"
        coordinates.at[index, "MATCH_NOTE"] = note
        coordinates.at[index, "MATCH_CONFIDENCE"] = 1.0

    for edit in edits:
        fields = _rows(coordinates)
        action = str(edit.get("action") or "change")
        row, line = edit.get("row", -1), edit.get("line", -1)
        text = str(edit.get("annotation") or "").strip()
        if action in ("change", "remove", "move") and row not in fields.index:
            refuse(edit, "no such annotation")
            continue
        if action in ("change", "add") and not _ANNOTATION.match(text):
            refuse(edit, "not a valid annotation")
            continue

        if action == "change":
            before = str(coordinates.at[row, "A_TEXT"])
            if before == text:
                refuse(edit, f"page {int(coordinates.at[row, 'PAGE_NO'])} already shows {text} there")
                continue
            coordinates.at[row, "A_TEXT"] = text
            reviewed(row, "edited in chat")
            touched.add(int(coordinates.at[row, "PAGE_NO"]))
            applied.append({"action": "annotation_changed", "page": int(coordinates.at[row, "PAGE_NO"]),
                            "crf_text": str(coordinates.at[row, "ITM_TEXT"]), "before": before, "after": text})
            continue

        if action == "remove":
            page_no = int(coordinates.at[row, "PAGE_NO"])
            applied.append({"action": "annotation_removed", "page": page_no,
                            "crf_text": str(coordinates.at[row, "ITM_TEXT"]),
                            "before": str(coordinates.at[row, "A_TEXT"]), "after": ""})
            coordinates = coordinates.drop(index=row)
            touched.add(page_no)
            continue

        # add / move need a printed line and a free spot beside it
        page = page or _Page(root)
        anchor = page.line(line) if isinstance(line, int) and line >= 0 else None
        if anchor is None:
            refuse(edit, "no such printed line")
            continue
        anchor_page = int(anchor["PAGE_NO"])
        if action == "add":
            same = fields[(fields["PAGE_NO"].astype(int) == anchor_page) & (fields["A_TEXT"].astype(str) == text)]
            if not same.empty:
                refuse(edit, f"page {anchor_page} already has {text} (beside “{same.iloc[0]['ITM_TEXT']}”)")
                continue
            position = page.place(coordinates, anchor, text)
            if not position:
                refuse(edit, f"no free space beside “{anchor['ITM_TEXT']}” on page {anchor_page}")
                continue
            index = int(coordinates.index.max()) + 1 if not coordinates.empty else 0
            same_page = fields[fields["PAGE_NO"].astype(int) == anchor_page]
            new_row = dict.fromkeys(coordinates.columns, "")
            new_row.update({"PAGE_NO": anchor_page, "ITM_TEXT": str(anchor["ITM_TEXT"]), "POS1": position,
                            "A_TEXT": text, "ANNOTATION_TYPE": ANNOTATION_FIELD})
            if not same_page.empty:
                new_row.update({"DS_OID": same_page.iloc[0].get("DS_OID", ""),
                                "DS_NAME": same_page.iloc[0].get("DS_NAME", "")})
            coordinates = pd.concat([coordinates, pd.DataFrame([new_row], index=[index])])
            reviewed(index, "added in chat")
            touched.add(anchor_page)
            applied.append({"action": "annotation_added", "page": anchor_page, "crf_text": str(anchor["ITM_TEXT"]),
                            "before": "", "after": text, "position": position})
            continue

        if action == "move":
            current = str(coordinates.at[row, "A_TEXT"])
            position = page.place(coordinates, anchor, current, skip_row=row)
            if not position:
                refuse(edit, f"no free space beside “{anchor['ITM_TEXT']}” on page {anchor_page}")
                continue
            from_page, from_text = int(coordinates.at[row, "PAGE_NO"]), str(coordinates.at[row, "ITM_TEXT"])
            coordinates.at[row, "PAGE_NO"] = anchor_page
            coordinates.at[row, "ITM_TEXT"] = str(anchor["ITM_TEXT"])
            coordinates.at[row, "POS1"] = position
            reviewed(row, "moved in chat")
            touched.update({from_page, anchor_page})
            applied.append({"action": "annotation_moved", "page": anchor_page, "crf_text": str(anchor["ITM_TEXT"]),
                            "before": f"p{from_page}: beside “{from_text}”",
                            "after": f"p{anchor_page}: beside “{anchor['ITM_TEXT']}”",
                            "annotation": current, "position": position})

    if touched:
        page = page or _Page(root)
        for page_no in sorted(touched):
            coordinates = _relayout_headers(coordinates, page_no, page.geometry, page.items)
        # the workbook's own order: per page, form rows, then banners, then fields
        rank = coordinates["ANNOTATION_TYPE"].astype(str).map({ANNOTATION_HEADER: 1, ANNOTATION_FIELD: 2}).fillna(0)
        order = pd.DataFrame({"page": coordinates["PAGE_NO"].astype(int), "rank": rank,
                              "seq": range(len(coordinates))}, index=coordinates.index)
        coordinates = coordinates.loc[order.sort_values(["page", "rank", "seq"], kind="stable").index]
    return coordinates, applied, refused


def answer(root: Path, message: str, study: str = "Study", client: Any = "auto") -> dict[str, Any]:
    workbook = root / "out" / "aCRF_Config.xlsx"
    if not workbook.is_file():
        return {"reply": "Run the annotation first — then ask me anything about the result.",
                "edits_applied": 0, "changes": []}
    coordinates = read_config_workbook(workbook)
    fields = _rows(coordinates)

    model = _client() if client == "auto" else client
    if model is None:
        if _REVIEW_WORDS.search(message):
            return {"reply": _review_reply(fields), "edits_applied": 0, "changes": []}
        return {"reply": "I can list what needs review right now. Plain-English edits need the AI "
                         "assistant, which isn't switched on for this workspace.", "edits_applied": 0,
                "changes": []}

    table = [
        {"id": int(index), "page": int(row["PAGE_NO"]), "crf_text": str(row["ITM_TEXT"])[:80],
         "annotation": str(row["A_TEXT"]), "matched": str(row.get("MATCH_METHOD", ""))}
        for index, row in fields.iterrows()
    ]
    named = pages_named(message)
    page = _Page(root) if named else None
    printed = page.lines(named) if page else []
    user = f"Study {study}. Annotations:\n{json.dumps(table)}\n\n"
    if printed:
        user += f"Printed lines on page(s) {', '.join(map(str, sorted(named)))}:\n{json.dumps(printed)}\n\n"
    response = model.json_call(system=_SYSTEM, user=user + f"User: {message}", schema=_SCHEMA, cache_salt=None) or {}
    reply = str(response.get("reply") or "Sorry — I couldn't work that out. Try rephrasing?")

    coordinates, changes, refused = apply_edits(root, coordinates, list(response.get("edits") or ()), page)
    if refused:
        # the model's reply may claim an edit that was not made: say so plainly
        reply += "\n\nNot applied: " + "; ".join(r["reason"] for r in refused) + "."
    if not changes:
        return {"reply": reply, "edits_applied": 0, "changes": refused}

    sheets = pd.read_excel(workbook, sheet_name=None, engine="openpyxl")
    write_config_workbook(workbook, coordinates, sheets.get(UNMAPPED_SHEET), sheets.get(FILENAMES_SHEET))
    try:
        result = annotate_crf(
            crf=root / "inputs" / "crf", config=workbook, output_dir=root / "out",
            design=root / "inputs" / "design", study=study, author="Vajrra aCRF",
        )
    except CrfAnnotationError as exc:
        return {"reply": f"{reply}\n\nI couldn't re-render the CRF: {exc}", "edits_applied": len(changes),
                "changes": changes + refused}
    data = result.to_dict()
    data.pop("acrf", None)
    data.pop("config", None)
    return {"reply": reply, "edits_applied": len(changes), "changes": changes + refused, "result": data}
