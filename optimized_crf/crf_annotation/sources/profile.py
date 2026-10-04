"""Profile-driven adapter for any tabular or JSON study-design export.

A *profile* is a small JSON document that says which sheet/key holds forms,
which column/field holds the form OID, and so on. Supporting a new export
format becomes a data change, not a code change — and a profile is reviewable,
diffable and version-controlled, which is exactly what you want in a validated
pipeline.

This is also the artefact `crf_annotation.ai.infer_profile` produces: Claude
looks at an unfamiliar export once and proposes a profile, a human checks it,
and every run afterwards is deterministic parsing with no model in the loop.

Profile shape::

    {
      "name": "Veeva Vault CDMS study design",
      "format": "json",                       # "json" | "tabular"
      "match": {"filename_contains": ["study_design"], "requires_keys": ["forms"]},
      "forms":       {"path": "forms",  "oid": "name", "label": "label"},
      "fields":      {"path": "items",  "oid": "name", "form_oid": "form",
                      "label": "prompt"},
      "visits":      {"path": "events", "oid": "name", "label": "label",
                      "order": "sequence"},
      "visit_forms": {"path": "event_forms", "visit_oid": "event",
                      "form_oid": "form"}
    }

For ``"format": "tabular"`` each section takes ``"sheet"`` instead of
``"path"`` and the field names are column headers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import pandas as pd

from ..design import DesignError, Field, Form, StudyDesign, Visit, VisitForm, build
from ..logging_utils import get_logger
from .base import peek, register

logger = get_logger()

TABULAR_SUFFIXES = (".xlsx", ".xls", ".csv")
JSON_SUFFIXES = (".json",)


def _dig(data: Any, path: str) -> Any:
    """Resolve a dotted path such as `study_design.forms`."""
    current = data
    for part in (p for p in path.split(".") if p):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, Sequence) and not isinstance(current, str):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _int(value: Any, fallback: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class Profile:
    """A parsed profile document."""

    name: str
    format: str
    spec: Mapping[str, Any]
    path: Optional[Path] = None

    @classmethod
    def load(cls, path: Path) -> Profile:
        try:
            spec = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DesignError(f"Profile {path.name} is not valid JSON: {exc}") from exc
        return cls.from_dict(spec, path)

    @classmethod
    def from_dict(cls, spec: Mapping[str, Any], path: Optional[Path] = None) -> Profile:
        name = str(spec.get("name") or (path.stem if path else "custom profile"))
        fmt = str(spec.get("format") or "json").lower()
        if fmt not in {"json", "tabular"}:
            raise DesignError(f"Profile {name}: format must be 'json' or 'tabular'")
        for section in ("forms", "fields"):
            if not isinstance(spec.get(section), Mapping):
                raise DesignError(f"Profile {name}: missing '{section}' section")
        return cls(name=name, format=fmt, spec=spec, path=path)

    # -- matching -----------------------------------------------------------
    def score(self, path: Path) -> float:
        suffixes = TABULAR_SUFFIXES if self.format == "tabular" else JSON_SUFFIXES
        if path.suffix.lower() not in suffixes:
            return 0.0

        rules = self.spec.get("match") or {}
        score = 0.35
        contains = rules.get("filename_contains") or []
        if isinstance(contains, str):
            contains = [contains]
        if contains:
            if not any(token.lower() in path.name.lower() for token in contains):
                return 0.0
            score += 0.3

        required = rules.get("requires_keys") or []
        if isinstance(required, str):
            required = [required]
        if required:
            head = peek(path, 65536)
            if not all(f'"{key}"' in head for key in required):
                return 0.0
            score += 0.3
        return min(score, 1.0)


class ProfileSource:
    """Reads one export format, as described by a `Profile`."""

    extensions = TABULAR_SUFFIXES + JSON_SUFFIXES

    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.name = profile.name

    def sniff(self, path: Path) -> float:
        return self.profile.score(Path(path))

    def load(self, path: Path) -> StudyDesign:
        rows = self._read_sections(Path(path))
        spec = self.profile.spec

        forms = [
            Form(oid=oid, label=_text(row.get(spec["forms"].get("label"))) or oid,
                 order=_int(row.get(spec["forms"].get("order")), index), source_oid=oid)
            for index, row in enumerate(rows.get("forms", ()))
            if (oid := _text(row.get(spec["forms"].get("oid"))))
        ]
        fields = [
            Field(oid=oid,
                  form_oid=_text(row.get(spec["fields"].get("form_oid"))),
                  label=_text(row.get(spec["fields"].get("label"))) or oid,
                  order=_int(row.get(spec["fields"].get("order")), index),
                  source_oid=oid)
            for index, row in enumerate(rows.get("fields", ()))
            if (oid := _text(row.get(spec["fields"].get("oid"))))
        ]

        visits: list[Visit] = []
        if "visits" in spec:
            section = spec["visits"]
            visits = [
                Visit(oid=oid, label=_text(row.get(section.get("label"))) or oid,
                      order=_int(row.get(section.get("order")), index))
                for index, row in enumerate(rows.get("visits", ()))
                if (oid := _text(row.get(section.get("oid"))))
            ]

        visit_forms: list[VisitForm] = []
        if "visit_forms" in spec:
            section = spec["visit_forms"]
            visit_forms = [
                VisitForm(visit_oid=visit, form_oid=form)
                for row in rows.get("visit_forms", ())
                if (visit := _text(row.get(section.get("visit_oid"))))
                and (form := _text(row.get(section.get("form_oid"))))
            ]

        if not forms:
            raise DesignError(f"Profile '{self.name}' matched {path.name} but found no forms")
        return build(f"{self.name} (profile)", forms, fields, visits, visit_forms)

    # -- reading ------------------------------------------------------------
    def _read_sections(self, path: Path) -> dict[str, list[Mapping[str, Any]]]:
        sections = [s for s in ("forms", "fields", "visits", "visit_forms")
                    if isinstance(self.profile.spec.get(s), Mapping)]
        if self.profile.format == "json":
            return self._read_json(path, sections)
        return self._read_tabular(path, sections)

    def _read_json(self, path: Path, sections: Iterable[str]) -> dict[str, list[Mapping[str, Any]]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DesignError(f"Invalid JSON: {exc}") from exc

        out: dict[str, list[Mapping[str, Any]]] = {}
        for section in sections:
            located = _dig(data, str(self.profile.spec[section].get("path", section)))
            if located is None:
                out[section] = []
            elif isinstance(located, Mapping):
                # A dict keyed by OID: fold the key in under the configured name.
                key_name = self.profile.spec[section].get("oid", "oid")
                out[section] = [
                    {**value, key_name: key} if isinstance(value, Mapping) else {key_name: key}
                    for key, value in located.items()
                ]
            elif isinstance(located, Sequence):
                out[section] = [row for row in located if isinstance(row, Mapping)]
            else:
                out[section] = []
        return out

    def _read_tabular(self, path: Path, sections: Iterable[str]) -> dict[str, list[Mapping[str, Any]]]:
        out: dict[str, list[Mapping[str, Any]]] = {}
        if path.suffix.lower() == ".csv":
            frames = {"": pd.read_csv(path)}
        else:
            with pd.ExcelFile(path) as book:
                frames = {name: book.parse(name) for name in book.sheet_names}

        lookup = {name.strip().lower(): frame for name, frame in frames.items()}
        for section in sections:
            sheet = str(self.profile.spec[section].get("sheet", section)).strip().lower()
            frame = lookup.get(sheet)
            if frame is None:
                out[section] = []
                continue
            frame = frame.rename(columns={c: str(c).strip() for c in frame.columns})
            out[section] = frame.to_dict("records")
        return out


def load_profiles(directory: Path) -> list[ProfileSource]:
    """Load and register every `*.profile.json` in `directory`."""
    adapters: list[ProfileSource] = []
    directory = Path(directory)
    if not directory.is_dir():
        return adapters
    for path in sorted(directory.glob("*.profile.json")):
        try:
            adapter = ProfileSource(Profile.load(path))
        except DesignError as exc:
            logger.warning("Ignoring profile %s: %s", path.name, exc)
            continue
        register(adapter)
        adapters.append(adapter)
        logger.info("Registered study-design profile '%s' from %s", adapter.name, path.name)
    return adapters
