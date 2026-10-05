"""Run orchestration — the one place that knows the order of operations."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Union

import pandas as pd

from .annotate import CrfRenderer
from .binding import AutoBinder, BannerBinder, HeaderNameBinder, SequentialBinder
from .bookmarks import add_outline
from .config_workbook import (
    ConfigWorkbookError,
    build_coordinate_table,
    build_file_info,
    read_config_workbook,
    write_config_workbook,
)
from .constants import FILENAMES_SHEET, METHOD_NOT_SUBMITTED, UNMAPPED_SHEET
from .job import (
    BindingOptions,
    CrfAnnotationError,
    CrfJob,
    InputError,
    MatchingOptions,
    PathLike,
    RunMode,
)
from .layout import place_annotations
from .logging_utils import configure_logging, get_logger
from .matching import refine
from .merge import (
    apply_annotations,
    build_form_fields,
    mark_not_submitted,
    match_fields,
    unmapped_variables,
)
from .pdf_text import extract_items
from .sources import load_study_design
from .specs import SpecCatalog
from .workspace import Workspace

logger = get_logger()


class PipelineError(CrfAnnotationError):
    """A run could not produce a usable annotated CRF."""


@dataclass
class AnnotationResult:
    """What a run produced. `to_dict()` is the agent-facing shape."""

    acrf: Path
    config: Path
    study: str
    mode: str
    annotations: int = 0
    pages: int = 0
    forms: int = 0
    fields: int = 0
    needs_review: int = 0
    not_submitted: int = 0
    unmapped_variables: int = 0
    annotations_dropped: int = 0
    study_design_source: str = ""
    match_methods: dict = field(default_factory=dict)
    proposed_specs: dict = field(default_factory=dict)
    """Set when the specs were drafted from the design: how many mappings were
    proposed by rule and by model. Every one of them needs a reviewer."""

    def to_dict(self) -> dict:
        return {
            "acrf": str(self.acrf),
            "config": str(self.config),
            "study": self.study,
            "mode": self.mode,
            "study_design_source": self.study_design_source,
            "pages": self.pages,
            "forms": self.forms,
            "fields": self.fields,
            "annotations": self.annotations,
            "annotations_dropped": self.annotations_dropped,
            "match_methods": self.match_methods,
            "needs_review": self.needs_review,
            "not_submitted": self.not_submitted,
            "unmapped_variables": self.unmapped_variables,
            "proposed_specs": self.proposed_specs,
        }


def annotate_crf(
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
) -> AnnotationResult:
    """Annotate a blank CRF with SDTM mappings.

    Two ways to call it:

    **First run** — derive annotations from the mapping specs and the EDC
    study design::

        annotate_crf(crf="blank.pdf", specs="specs/", design="design.xml",
                     output_dir="out/", study="STUDY123")

    **Rerun** — replay a config workbook, including a reviewer's corrections::

        annotate_crf(crf="blank.pdf", config="out/aCRF_Config.xlsx",
                     output_dir="out/")

    `specs` and `design` each accept a file, a directory, or a list of either.

    Raises `InputError` for bad inputs and `PipelineError` when a run cannot
    produce a usable result. Nothing is written unless the run succeeds.
    """
    job = CrfJob.build(
        crf=crf, output_dir=output_dir, specs=specs, design=design, config=config,
        study=study, author=author, log_dir=log_dir, matching=matching, binding=binding,
    )
    if job.log_dir is not None:
        # Only take over logging when asked to write a log file; a caller that
        # has already configured logging (the CLI, or a host application) keeps
        # its own level and handlers.
        configure_logging(job.log_dir)
    return run(job)


def run(job: CrfJob) -> AnnotationResult:
    """Execute an already-built `CrfJob`."""
    logger.info("Starting aCRF run for %s (%s)", job.study_name, job.mode)

    with Workspace(job.output_dir) as workspace:
        design_source = ""
        stats = None
        counts: dict = {}

        if job.mode is RunMode.FIRST_RUN:
            (coordinates, unmapped, file_info, visit_matrix,
             design_source, stats, counts) = _from_specs(job)
        else:
            coordinates, unmapped, file_info = _from_config(job)
            visit_matrix = _visit_matrix_for_rerun(job)
            counts = {}

        shutil.copyfile(job.crf, workspace.working_pdf)  # contents only, see workspace.py
        bookmarked = workspace.temp_dir / "acrf_bookmarked.pdf"
        add_outline(
            workspace.working_pdf, bookmarked, coordinates, visit_matrix,
            job.study_name, author=job.author,
        )
        CrfRenderer(bookmarked, workspace.annotated_pdf).render(coordinates)
        write_config_workbook(workspace.config_workbook, coordinates, unmapped, file_info)
        outputs = workspace.publish()

    result = _summarise(job, coordinates, unmapped, outputs, design_source, stats, counts)
    logger.info("Run complete: %d annotation(s) on %d page(s)%s",
                result.annotations, result.pages,
                f", {result.needs_review} needing review" if result.needs_review else "")
    return result


# --- first run -------------------------------------------------------------
def _from_specs(job: CrfJob):
    design = load_study_design(job.require_design())
    spec_files = job.specs
    proposed: dict = {}
    if not spec_files:
        spec_files, proposed = _propose_specs(job, design)
    catalog = SpecCatalog.load(spec_files, include_derived=job.matching.include_derived)
    if catalog.mappings.empty:
        raise PipelineError(
            "No usable mappings found in the spec files; nothing could be annotated"
        )

    items, geometry = extract_items(job.crf)
    if items.empty:
        raise PipelineError(
            f"No extractable text in {job.crf.name}. A scanned PDF with no text "
            "layer cannot be annotated."
        )

    form_fields = build_form_fields(design)
    matched = match_fields(items, form_fields, design, binder=_binder(job.binding))
    matched = refine(
        matched, form_fields,
        resolvers=_resolvers(job),
        min_confidence=job.matching.min_confidence,
    )
    # Proposed specs are keyed by form OID by construction, so a field only
    # ever takes its own form's annotation — never a look-alike from another
    # form that shares the same item (a reused "Visit Date", say).
    scope = "strict" if proposed and job.matching.dataset_scope == "auto" else job.matching.dataset_scope
    annotated = apply_annotations(matched, catalog, dataset_scope=scope)

    # Record the mapping gap before filling it in, so the `unmap` sheet still
    # reports which variables have no SDTM target.
    unmapped = unmapped_variables(annotated)
    if job.matching.mark_not_submitted:
        annotated = mark_not_submitted(annotated)

    placed, stats = place_annotations(annotated, geometry)

    coordinates = build_coordinate_table(placed, catalog, geometry)
    if coordinates.empty:
        raise PipelineError(
            "No CRF field matched a mapping. Check that the spec source datasets "
            "line up with the EDC form and field names."
        )

    file_info = build_file_info({
        "Study_Design_System": [design.source_system],
        "Study_Design_File": job.design_names,
        "Spec_File": job.spec_names,
    })
    counts = {"forms": len(design.forms), "fields": len(design.fields), "proposed": proposed}
    return (coordinates, unmapped, file_info, design.visit_frame,
            design.source_system, stats, counts)


# --- rerun -----------------------------------------------------------------
def _from_config(job: CrfJob):
    if job.config is None:  # pragma: no cover - guarded by CrfJob.build
        raise InputError("Rerun requested without a config workbook")

    coordinates = read_config_workbook(job.config)
    if not (coordinates["A_TEXT"].astype(str).str.strip() != "").any():
        raise ConfigWorkbookError(
            f"{job.config.name} contains no annotation text; nothing to draw"
        )
    return coordinates, _optional_sheet(job.config, UNMAPPED_SHEET), \
        _optional_sheet(job.config, FILENAMES_SHEET)


def _visit_matrix_for_rerun(job: CrfJob) -> pd.DataFrame:
    """Visit bookmarks on a rerun are best-effort: the design may not be around."""
    if not job.design:
        return pd.DataFrame()
    try:
        return load_study_design(job.design).visit_frame
    except Exception as exc:  # noqa: BLE001 - annotations matter, visits are extra
        logger.warning("No visit bookmarks on this rerun: %s", exc)
        return pd.DataFrame()


def _optional_sheet(path: Path, sheet: str) -> pd.DataFrame:
    """Carry a supporting sheet through a rerun; absence is not an error."""
    try:
        return pd.read_excel(path, sheet_name=sheet, engine="openpyxl")
    except ValueError:
        return pd.DataFrame()
    except OSError as exc:
        logger.warning("Could not read sheet %s from %s: %s", sheet, path, exc)
        return pd.DataFrame()


# --- strategies ------------------------------------------------------------
def _binder(options: BindingOptions):
    binders = [
        BannerBinder(prefix=options.banner_prefix) if options.banner_prefix else BannerBinder(),
        HeaderNameBinder(),
    ]
    fallback = SequentialBinder() if options.allow_sequential_fallback else None
    if fallback is not None:
        logger.warning("Sequential form binding is enabled as a fallback; if it is "
                       "used, every annotation on the run needs review")
    return AutoBinder(binders, fallback=fallback)


def _ai_settings(job: CrfJob):
    from .ai import AiSettings

    options = job.matching
    settings = AiSettings.from_env(model=options.model, effort=options.effort)
    settings.enabled = bool(options.ai)
    settings.cache_dir = Path(options.cache_dir) if options.cache_dir else (
        (job.log_dir or job.output_dir) / "ai_cache"
    )
    return settings


def _propose_specs(job: CrfJob, design):
    """No specs given: draft them into `<out>/proposed_specs/` for review."""
    from .ai.client import ClaudeClient
    from .ai.propose_specs import propose_mappings, write_proposal

    settings = _ai_settings(job)
    client = ClaudeClient(settings) if settings.enabled and not job.matching.exact_only else None
    proposal = propose_mappings(design, client)
    if not proposal.mappings:
        raise PipelineError(
            "No mapping specs were given and none could be proposed from the study design"
        )
    files = write_proposal(proposal, job.output_dir / "proposed_specs")
    return files, proposal.counts


def _resolvers(job: CrfJob):
    options = job.matching
    if options.exact_only:
        logger.info("Matching restricted to exact — no fuzzy or AI tiers")
        return ()

    from .ai import AiSettings, build_resolvers

    settings = AiSettings.from_env(model=options.model, effort=options.effort)
    settings.enabled = bool(options.ai)
    settings.cache_dir = Path(options.cache_dir) if options.cache_dir else (
        (job.log_dir or job.output_dir) / "ai_cache"
    )
    return build_resolvers(settings, fuzzy_threshold=options.fuzzy_threshold)


# --- reporting -------------------------------------------------------------
def _summarise(job, coordinates, unmapped, outputs, design_source, stats,
               counts: Mapping) -> AnnotationResult:
    fields = coordinates[coordinates["ANNOTATION_TYPE"] == "Field"] \
        if "ANNOTATION_TYPE" in coordinates.columns else coordinates
    methods = fields["MATCH_METHOD"].astype(str) if "MATCH_METHOD" in fields.columns \
        else pd.Series(dtype=str)

    return AnnotationResult(
        acrf=outputs.pdf_path,
        config=outputs.config_path,
        study=job.study_name,
        mode=str(job.mode),
        study_design_source=design_source,
        forms=int(counts.get("forms", 0)),
        fields=int(counts.get("fields", 0)),
        pages=int(coordinates["PAGE_NO"].nunique()) if not coordinates.empty else 0,
        annotations=int((coordinates["A_TEXT"].astype(str) != "").sum()),
        unmapped_variables=int(len(unmapped)),
        annotations_dropped=stats.dropped if stats else 0,
        match_methods={k: int(v) for k, v in methods[methods != ""].value_counts().items()},
        proposed_specs=dict(counts.get("proposed") or {}),
        not_submitted=int((methods == METHOD_NOT_SUBMITTED).sum()),
        # A NOT SUBMITTED mark is a deliberate statement, not a guess, so it
        # does not count toward the review burden.
        needs_review=int((~methods.isin(
            ["", "exact", "reviewed", METHOD_NOT_SUBMITTED])).sum()),
    )
