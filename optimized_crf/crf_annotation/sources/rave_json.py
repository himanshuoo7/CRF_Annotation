"""Medidata Rave — the pre-extracted `source_metadata_*.json`.

A flat JSON object with `forms` and `fields` arrays, extracted from a Rave
build. It carries no visit information.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from ..design import DesignError, Field, Form, StudyDesign, build
from ..logging_utils import get_logger
from .base import peek, register, suffix_score

logger = get_logger()


class RaveJsonSource:
    name = "Rave source_metadata JSON"
    extensions = (".json",)

    def sniff(self, path: Path) -> float:
        score = suffix_score(path, self.extensions)
        if not score:
            return 0.0
        if path.name.lower().startswith("source_metadata_"):
            score += 0.4
        head = peek(path)
        if '"form_label"' in head or '"field_label"' in head:
            score += 0.4
        elif '"forms"' in head and '"fields"' in head:
            score += 0.2
        return min(score, 1.0)

    def load(self, path: Path) -> StudyDesign:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise DesignError(f"Invalid JSON: {exc}") from exc
        except OSError as exc:
            raise DesignError(f"Cannot read file: {exc}") from exc

        if not isinstance(data, Mapping):
            raise DesignError("Expected a JSON object at the top level")
        missing = [key for key in ("forms", "fields") if key not in data]
        if missing:
            raise DesignError(f"Missing section(s): {missing}")

        forms = [
            Form(oid=str(row.get("form", "")).strip(),
                 label=str(row.get("form_label", "")).strip(),
                 order=index)
            for index, row in enumerate(data.get("forms") or ())
            if isinstance(row, Mapping) and row.get("form")
        ]
        fields = [
            Field(oid=str(row.get("field", "")).strip(),
                  form_oid=str(row.get("form", "")).strip(),
                  label=str(row.get("field_label", "")).strip(),
                  order=index)
            for index, row in enumerate(data.get("fields") or ())
            if isinstance(row, Mapping) and row.get("field")
        ]
        return build(self.name, forms, fields, study_name=str(data.get("study", "")))


register(RaveJsonSource())
