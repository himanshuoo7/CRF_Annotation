"""Job definition — explicit inputs, no host-platform vocabulary.

A job is described by what the work actually needs — a CRF, some specs, a
study design, and somewhere to put the output — and is validated once, here,
so no later stage has to re-derive or re-check an input.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

from .constants import OUTPUT_CONFIG_NAME
from .logging_utils import get_logger

logger = get_logger()

PathLike = Union[str, Path]

SPEC_SUFFIXES = (".json",)
DESIGN_SUFFIXES = (".json", ".xml", ".odm", ".xlsx", ".xls", ".csv")


class CrfAnnotationError(Exception):
    """Base class for every error this package raises deliberately."""


class InputError(CrfAnnotationError):
    """A required input is missing, unreadable, or contradictory."""


class RunMode(enum.Enum):
    FIRST_RUN = "first_run"   # build annotations from specs + study design
    RERUN = "rerun"           # replay a reviewed config workbook

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.value


def _resolve(value: Optional[Union[PathLike, Iterable[PathLike]]],
             suffixes: Sequence[str], label: str) -> tuple[Path, ...]:
    """Accept a file, a directory, or any iterable of either."""
    if value is None:
        return ()
    if isinstance(value, (str, Path)):
        value = [value]

    found: list[Path] = []
    for item in value:
        path = Path(item).expanduser()
        if path.is_dir():
            for suffix in suffixes:
                found.extend(sorted(path.glob(f"*{suffix}")))
        elif path.is_file():
            found.append(path)
        else:
            raise InputError(f"{label} not found: {path}")

    unique: list[Path] = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return tuple(unique)


@dataclass
class MatchingOptions:
    """How CRF text is matched to EDC fields."""

    ai: bool = True
    """Use a language model for text the deterministic tiers cannot resolve."""
    exact_only: bool = False
    """Fully deterministic: no fuzzy tier, no model. For validation runs."""
    fuzzy_threshold: float = 0.88
    min_confidence: float = 0.75
    dataset_scope: str = "auto"        # auto | strict | off
    model: Optional[str] = None
    effort: Optional[str] = None
    cache_dir: Optional[PathLike] = None
    include_derived: bool = False
    """Annotate SDTM targets that are derived or assigned rather than
    collected (`USUBJID`, `--SEQ`, `EPOCH`). Off by default — CDISC
    conventions document those in define.xml instead."""


@dataclass
class BindingOptions:
    """How each CRF page is matched to the form it belongs to."""

    banner_prefix: Optional[str] = None
    """Literal that precedes a form name on the page, e.g. `"Form: "`."""
    allow_sequential_fallback: bool = False
    """Last resort: assume pages follow the design's form order. Guesses."""


@dataclass
class CrfJob:
    """A validated, ready-to-run job."""

    crf: Path
    output_dir: Path
    mode: RunMode
    specs: tuple[Path, ...] = ()
    design: tuple[Path, ...] = ()
    config: Optional[Path] = None
    study: str = ""
    author: str = ""
    log_dir: Optional[Path] = None
    matching: MatchingOptions = field(default_factory=MatchingOptions)
    binding: BindingOptions = field(default_factory=BindingOptions)

    @classmethod
    def build(
        cls,
        crf: PathLike,
        output_dir: PathLike,
        specs: Optional[Union[PathLike, Iterable[PathLike]]] = None,
        design: Optional[Union[PathLike, Iterable[PathLike]]] = None,
        config: Optional[PathLike] = None,
        study: str = "",
        author: str = "",
        log_dir: Optional[PathLike] = None,
        matching: Optional[MatchingOptions] = None,
        binding: Optional[BindingOptions] = None,
    ) -> CrfJob:
        crf_path = Path(crf).expanduser()
        if crf_path.is_dir():
            pdfs = sorted(crf_path.glob("*.pdf"))
            if not pdfs:
                raise InputError(f"No PDF found in {crf_path}")
            if len(pdfs) > 1:
                logger.warning("%d PDFs in %s; using %s", len(pdfs), crf_path, pdfs[0].name)
            crf_path = pdfs[0]
        if not crf_path.is_file():
            raise InputError(f"Blank CRF not found: {crf_path}")

        out = Path(output_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)

        config_path = cls._resolve_config(config, out)
        spec_files = _resolve(specs, SPEC_SUFFIXES, "spec file")
        design_files = _resolve(design, DESIGN_SUFFIXES, "study design file")

        if config_path is not None:
            mode = RunMode.RERUN
        elif spec_files:
            mode = RunMode.FIRST_RUN
            if not design_files:
                raise InputError(
                    "A first run needs the EDC study design as well as the mapping "
                    "specs. Pass design= (a Rave ALS workbook, a CDISC ODM-XML "
                    "export, or any format a registered adapter reads)."
                )
        else:
            raise InputError(
                "Nothing to do: pass specs= and design= for a first run, or "
                "config= to replay a reviewed config workbook."
            )

        job = cls(
            crf=crf_path,
            output_dir=out,
            mode=mode,
            specs=spec_files,
            design=design_files,
            config=config_path,
            study=study.strip(),
            author=author.strip(),
            log_dir=Path(log_dir).expanduser() if log_dir else None,
            matching=matching or MatchingOptions(),
            binding=binding or BindingOptions(),
        )
        logger.info("Job: mode=%s, crf=%s, %d spec(s), %d design file(s)",
                    job.mode, job.crf.name, len(job.specs), len(job.design))
        return job

    @staticmethod
    def _resolve_config(config: Optional[PathLike], output_dir: Path) -> Optional[Path]:
        if config is None:
            return None
        path = Path(config).expanduser()
        if path.is_dir():
            path = path / OUTPUT_CONFIG_NAME
        if not path.is_file():
            raise InputError(f"Config workbook not found: {path}")
        return path

    # -- accessors ----------------------------------------------------------
    def require_design(self) -> tuple[Path, ...]:
        if not self.design:
            raise InputError("No study-design metadata was supplied")
        return self.design

    @property
    def study_name(self) -> str:
        return self.study or self.crf.stem

    @property
    def spec_names(self) -> list[str]:
        return [p.name for p in self.specs]

    @property
    def design_names(self) -> list[str]:
        return [p.name for p in self.design]
