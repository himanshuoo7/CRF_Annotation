"""Command line entry point.

    crf-annotate --crf blank.pdf --specs specs/ --design design.xml --out out/
    crf-annotate --crf blank.pdf --config out/aCRF_Config.xlsx --out out/

`--json` prints a machine-readable result and is what an agent should use.
Exit status is 0 on success, 1 on a failed run, 2 on bad inputs.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from .job import BindingOptions, CrfAnnotationError, InputError, MatchingOptions
from .logging_utils import configure_logging
from .pipeline import annotate_crf


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crf-annotate",
        description="Annotate a blank CRF with SDTM mappings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--crf", required=True, metavar="PDF",
                        help="Blank CRF PDF (or a directory containing one)")
    parser.add_argument("--out", required=True, metavar="DIR",
                        help="Where to write acrf.pdf and aCRF_Config.xlsx")
    parser.add_argument("--specs", nargs="*", metavar="PATH", default=None,
                        help="SDTM mapping spec JSON files, or a directory of them")
    parser.add_argument("--design", nargs="*", metavar="PATH", default=None,
                        help="EDC study design export(s): Rave ALS, CDISC ODM-XML, …")
    parser.add_argument("--config", metavar="XLSX", default=None,
                        help="Replay a reviewed config workbook instead of a first run")
    parser.add_argument("--study", default="", help="Study name for PDF metadata and bookmarks")
    parser.add_argument("--author", default="", help="PDF /Author metadata")
    parser.add_argument("--log-dir", default=None, help="Directory for the run log")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--quiet", action="store_true", help="Errors only on the console")

    group = parser.add_argument_group("matching")
    group.add_argument("--no-ai", action="store_true",
                       help="Disable the model tier; deterministic tiers only")
    group.add_argument("--exact-only", action="store_true",
                       help="Fully deterministic: no fuzzy tier either")
    group.add_argument("--min-confidence", type=float, default=0.75)
    group.add_argument("--fuzzy-threshold", type=float, default=0.88)
    group.add_argument("--dataset-scope", choices=["auto", "strict", "off"], default="auto")
    group.add_argument("--model", default=None, help="Model id for the AI tier")
    group.add_argument("--no-mark-not-submitted", action="store_true",
                       help="Do not annotate unmapped collected fields with "
                            "NOT SUBMITTED (FDA SDTCG 4.1.4.6 asks for it)")
    group.add_argument("--include-derived", action="store_true",
                       help="Also annotate derived/assigned SDTM variables "
                            "(USUBJID, --SEQ, EPOCH); off by default")

    group = parser.add_argument_group("page/form binding")
    group.add_argument("--banner-prefix", default=None,
                       help='Literal preceding a form name on the page, e.g. "Form: "')
    group.add_argument("--allow-sequential-fallback", action="store_true",
                       help="Last resort: assume pages follow form order (guesses)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    import logging
    configure_logging(args.log_dir, level=logging.ERROR if args.quiet else logging.INFO)

    try:
        result = annotate_crf(
            crf=args.crf,
            output_dir=args.out,
            specs=args.specs,
            design=args.design,
            config=args.config,
            study=args.study,
            author=args.author,
            log_dir=args.log_dir,
            matching=MatchingOptions(
                ai=not args.no_ai,
                exact_only=args.exact_only,
                min_confidence=args.min_confidence,
                fuzzy_threshold=args.fuzzy_threshold,
                dataset_scope=args.dataset_scope,
                model=args.model,
                include_derived=args.include_derived,
                mark_not_submitted=not args.no_mark_not_submitted,
            ),
            binding=BindingOptions(
                banner_prefix=args.banner_prefix,
                allow_sequential_fallback=args.allow_sequential_fallback,
            ),
        )
    except InputError as exc:
        _fail(str(exc), "InputError", args.json)
        return 2
    except CrfAnnotationError as exc:
        _fail(str(exc), type(exc).__name__, args.json)
        return 1
    except Exception as exc:  # noqa: BLE001 - the CLI must not traceback at a caller
        _fail(f"{type(exc).__name__}: {exc}", type(exc).__name__, args.json)
        return 1

    if args.json:
        json.dump({"status": "ok", **result.to_dict()}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"aCRF:   {result.acrf}")
        print(f"config: {result.config}")
        print(f"{result.annotations} annotation(s) on {result.pages} page(s)"
              + (f" from {result.fields} field(s) across {result.forms} form(s)"
                 if result.fields else ""))
        if result.match_methods:
            print("by method: " + ", ".join(f"{k}={v}" for k, v in
                                            sorted(result.match_methods.items())))
        if result.needs_review:
            print(f"{result.needs_review} annotation(s) need review "
                  "(not an exact match) — see the config workbook")
        if result.not_submitted:
            print(f"{result.not_submitted} field(s) annotated NOT SUBMITTED")
        if result.unmapped_variables:
            print(f"{result.unmapped_variables} unmapped variable(s)")
        if result.annotations_dropped:
            print(f"! {result.annotations_dropped} annotation(s) had no room on the page")
    return 0


def _fail(message: str, kind: str, as_json: bool) -> None:
    if as_json:
        json.dump({"status": "error", "error": kind, "message": message},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"error: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
