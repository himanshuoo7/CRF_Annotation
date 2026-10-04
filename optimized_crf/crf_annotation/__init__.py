"""SDTM annotated CRF (aCRF) generation.

Annotate a blank CRF PDF with the SDTM variables each field maps to, and
produce a reviewable config workbook alongside it.

    from crf_annotation import annotate_crf

    result = annotate_crf(
        crf="blank_crf.pdf",
        specs="specs/",
        design="study_design.xml",
        output_dir="out/",
        study="STUDY123",
    )
    print(result.acrf, result.needs_review)

Replay a reviewed workbook, corrections and all:

    annotate_crf(crf="blank_crf.pdf", config="out/aCRF_Config.xlsx",
                 output_dir="out/")
"""

from .job import (
    BindingOptions,
    CrfAnnotationError,
    CrfJob,
    InputError,
    MatchingOptions,
    RunMode,
)
from .pipeline import AnnotationResult, PipelineError, annotate_crf, run

__all__ = [
    "annotate_crf",
    "run",
    "AnnotationResult",
    "CrfJob",
    "MatchingOptions",
    "BindingOptions",
    "RunMode",
    "CrfAnnotationError",
    "InputError",
    "PipelineError",
]
__version__ = "1.0.0"
