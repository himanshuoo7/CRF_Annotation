"""The adapter contract plus the sniffing registry.

An adapter answers two questions: *can I read this file?* and *what study
design does it describe?* Adding support for a new EDC means adding one
module here — nothing else in the pipeline changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Protocol, Sequence, runtime_checkable

from ..design import DesignError, StudyDesign
from ..logging_utils import get_logger

logger = get_logger()


@runtime_checkable
class DesignSource(Protocol):
    """A reader for one EDC's study-design export."""

    name: str
    """Human-readable system name, e.g. 'Medidata Rave ALS'."""

    extensions: tuple[str, ...]
    """Lower-case file suffixes this adapter might handle, e.g. ('.xlsx',)."""

    def sniff(self, path: Path) -> float:
        """Confidence in [0, 1] that this adapter can read `path`.

        Cheap checks only — a suffix test, a sheet-name list, the first few
        hundred bytes. Never parse the whole file here.
        """

    def load(self, path: Path) -> StudyDesign:
        """Parse `path` into the canonical model. Raise `DesignError` on failure."""


_REGISTRY: list[DesignSource] = []


def register(adapter: DesignSource) -> DesignSource:
    """Add an adapter to the sniffing registry (idempotent by name)."""
    for index, existing in enumerate(_REGISTRY):
        if existing.name == adapter.name:
            _REGISTRY[index] = adapter
            return adapter
    _REGISTRY.append(adapter)
    return adapter


def registered() -> tuple[DesignSource, ...]:
    return tuple(_REGISTRY)


def identify(path: Path) -> list[tuple[float, DesignSource]]:
    """Score every adapter against `path`, best first."""
    scores: list[tuple[float, DesignSource]] = []
    for adapter in _REGISTRY:
        try:
            confidence = float(adapter.sniff(Path(path)))
        except Exception as exc:  # noqa: BLE001 - a broken sniff must not block others
            logger.debug("Adapter %s failed to sniff %s: %s", adapter.name, path, exc)
            continue
        if confidence > 0:
            scores.append((confidence, adapter))
    return sorted(scores, key=lambda item: item[0], reverse=True)


def load_study_design(
    paths: Sequence[Path],
    prefer: Optional[str] = None,
) -> StudyDesign:
    """Read the study design from one or more metadata files.

    Several exports split the design across files (fields in one, the visit
    matrix in another). Every file is offered to the registry and the results
    are merged, richest first.
    """
    candidates = [Path(p) for p in paths if p and Path(p).exists()]
    if not candidates:
        raise DesignError("No study-design metadata files were supplied")

    loaded: list[StudyDesign] = []
    failures: list[str] = []

    for path in candidates:
        ranked = identify(path)
        if prefer:
            ranked = [(c, a) for c, a in ranked if a.name == prefer] or ranked
        if not ranked:
            failures.append(f"{path.name}: no adapter recognised this file")
            continue

        confidence, adapter = ranked[0]
        try:
            design = adapter.load(path)
        except DesignError as exc:
            failures.append(f"{path.name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - surface as a readable failure
            failures.append(f"{path.name}: {adapter.name} adapter failed - {exc}")
            continue

        logger.info("Read %s with the %s adapter (confidence %.2f)",
                    path.name, adapter.name, confidence)
        loaded.append(design)

    if not loaded:
        raise DesignError(
            "Could not read a study design from any supplied file:\n  "
            + "\n  ".join(failures)
        )
    for failure in failures:
        logger.info("Skipped %s", failure)

    return merge(loaded).validate()


def merge(designs: Sequence[StudyDesign]) -> StudyDesign:
    """Combine partial designs. First non-empty section wins; visits accumulate."""
    if len(designs) == 1:
        return designs[0]

    ranked = sorted(designs, key=lambda d: (len(d.fields), len(d.forms)), reverse=True)
    primary = ranked[0]

    forms = {f.oid: f for f in primary.forms}
    fields = {(f.form_oid, f.oid): f for f in primary.fields}
    visits = {v.oid: v for v in primary.visits}
    visit_forms = set(primary.visit_forms)

    for design in ranked[1:]:
        for form in design.forms:
            forms.setdefault(form.oid, form)
        for item in design.fields:
            fields.setdefault((item.form_oid, item.oid), item)
        for visit in design.visits:
            visits.setdefault(visit.oid, visit)
        visit_forms.update(design.visit_forms)

    systems = " + ".join(dict.fromkeys(d.source_system for d in ranked))
    return StudyDesign(
        source_system=systems,
        forms=tuple(forms.values()),
        fields=tuple(fields.values()),
        visits=tuple(visits.values()),
        visit_forms=tuple(sorted(visit_forms, key=lambda vf: (vf.visit_oid, vf.form_oid))),
        study_name=next((d.study_name for d in ranked if d.study_name), ""),
        warnings=tuple(w for d in ranked for w in d.warnings),
    )


# --- helpers shared by adapters -------------------------------------------
def suffix_score(path: Path, extensions: Iterable[str], base: float = 0.3) -> float:
    return base if path.suffix.lower() in tuple(extensions) else 0.0


def peek(path: Path, size: int = 4096) -> str:
    """First `size` bytes as text — for cheap format sniffing."""
    try:
        with open(path, "rb") as handle:
            return handle.read(size).decode("utf-8", errors="replace")
    except OSError:
        return ""
