"""Medidata Rave — the Architect Loader Spreadsheet (ALS).

Sheets: `Forms`, `Fields`, `Folders`, and one `Matrix#<FolderOID>` per matrix.
The matrix sheets are a form-by-visit grid marked with `X`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

import pandas as pd

from ..design import DesignError, Field, Form, StudyDesign, Visit, VisitForm, build
from ..logging_utils import get_logger
from .base import register, suffix_score

logger = get_logger()

_MARK = "X"


def _first_column(frame: pd.DataFrame, *names: str) -> Optional[str]:
    upper = {str(c).strip().upper(): c for c in frame.columns}
    for name in names:
        if name in upper:
            return upper[name]
    return None


def _text(value: Any) -> str:
    return "" if value is None or pd.isna(value) else str(value).strip()


class RaveAlsSource:
    name = "Medidata Rave ALS"
    extensions = (".xlsx", ".xls")

    def sniff(self, path: Path) -> float:
        score = suffix_score(path, self.extensions)
        if not score:
            return 0.0
        try:
            with pd.ExcelFile(path) as book:
                sheets = {s.strip().lower() for s in book.sheet_names}
                matrices = [s for s in book.sheet_names if s.startswith("Matrix")]
        except Exception:
            return 0.0
        if "forms" in sheets and "fields" in sheets:
            score += 0.4
        if "folders" in sheets:
            score += 0.1
        if matrices:
            score += 0.2
        return min(score, 1.0)

    def load(self, path: Path) -> StudyDesign:
        try:
            with pd.ExcelFile(path) as book:
                sheets = book.sheet_names
                forms_df = self._sheet(book, sheets, "Forms")
                fields_df = self._sheet(book, sheets, "Fields")
                folders_df = self._sheet(book, sheets, "Folders")
                matrices = {
                    sheet: book.parse(sheet)
                    for sheet in sheets
                    if sheet.startswith("Matrix")
                }
        except DesignError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DesignError(f"Cannot read workbook: {exc}") from exc

        if forms_df.empty:
            raise DesignError("No usable 'Forms' sheet")

        forms = self._forms(forms_df)
        fields = self._fields(fields_df)
        visits, visit_forms = self._visits(folders_df, matrices)

        warnings = []
        if fields_df.empty:
            warnings.append("no 'Fields' sheet — field labels must come from another file")
        return build(self.name, forms, fields, visits, visit_forms, warnings=warnings)

    # -- sheets -------------------------------------------------------------
    @staticmethod
    def _sheet(book: pd.ExcelFile, sheets: list[str], name: str) -> pd.DataFrame:
        match = next((s for s in sheets if s.strip().lower() == name.lower()), None)
        return book.parse(match) if match else pd.DataFrame()

    @staticmethod
    def _forms(frame: pd.DataFrame) -> list[Form]:
        oid_col = _first_column(frame, "OID", "FORMOID", "DRAFTFORMOID")
        name_col = _first_column(frame, "DRAFTFORMNAME", "FORMNAME", "NAME", "LABEL")
        order_col = _first_column(frame, "ORDINAL", "ORDER", "ORDERNUMBER")
        if not oid_col:
            raise DesignError("'Forms' sheet has no OID column")
        return [
            Form(
                oid=_text(row[oid_col]),
                label=_text(row[name_col]) if name_col else _text(row[oid_col]),
                order=int(row[order_col]) if order_col and pd.notna(row[order_col]) else index,
            )
            for index, (_, row) in enumerate(frame.iterrows())
            if _text(row[oid_col])
        ]

    @staticmethod
    def _fields(frame: pd.DataFrame) -> list[Field]:
        if frame.empty:
            return []
        form_col = _first_column(frame, "FORMOID", "DRAFTFORMNAME", "FORM")
        oid_col = _first_column(frame, "FIELDOID", "OID", "NAME")
        label_col = _first_column(frame, "PRETEXT", "DRAFTFIELDNAME", "LABEL", "FIELDLABEL")
        order_col = _first_column(frame, "ORDINAL", "ORDER")
        if not (form_col and oid_col):
            logger.warning("'Fields' sheet lacks a form or field OID column; skipping fields")
            return []
        return [
            Field(
                oid=_text(row[oid_col]),
                form_oid=_text(row[form_col]),
                label=_text(row[label_col]) if label_col else _text(row[oid_col]),
                order=int(row[order_col]) if order_col and pd.notna(row[order_col]) else index,
            )
            for index, (_, row) in enumerate(frame.iterrows())
            if _text(row[oid_col]) and _text(row[form_col])
        ]

    @staticmethod
    def _visits(
        folders: pd.DataFrame,
        matrices: Mapping[str, pd.DataFrame],
    ) -> tuple[list[Visit], list[VisitForm]]:
        if folders.empty or not matrices:
            return [], []

        oid_col = _first_column(folders, "OID", "FOLDEROID")
        name_col = _first_column(folders, "FOLDERNAME", "NAME")
        order_col = _first_column(folders, "ORDINAL", "ORDER")
        parent_col = _first_column(folders, "PARENTFOLDEROID", "PARENTOID")
        if not oid_col:
            return [], []

        visits: list[Visit] = []
        parents: dict[str, str] = {}
        for index, (_, row) in enumerate(folders.iterrows()):
            oid = _text(row[oid_col])
            if not oid:
                continue
            parent = _text(row[parent_col]) if parent_col else ""
            parents[oid] = parent or oid
            visits.append(
                Visit(
                    oid=oid,
                    label=_text(row[name_col]) if name_col else oid,
                    order=int(row[order_col]) if order_col and pd.notna(row[order_col]) else index,
                    parent_oid=parent or oid,
                )
            )

        pairs: list[VisitForm] = []
        for sheet, frame in matrices.items():
            if frame.empty or len(frame.columns) < 2:
                continue
            matrix_id = sheet.split("#", 1)[1] if "#" in sheet else ""
            form_col = frame.columns[0]
            visit_cols = [
                c for c in frame.columns[1:]
                if c in parents and (not matrix_id or parents[c] == matrix_id)
            ]
            if not visit_cols:
                continue
            melted = frame.melt(id_vars=[form_col], value_vars=visit_cols,
                                var_name="visit", value_name="mark")
            marked = melted[melted["mark"].astype(str).str.strip().str.upper() == _MARK]
            pairs.extend(
                VisitForm(visit_oid=str(row["visit"]), form_oid=_text(row[form_col]))
                for _, row in marked.iterrows()
                if _text(row[form_col])
            )
        return visits, pairs


register(RaveAlsSource())
