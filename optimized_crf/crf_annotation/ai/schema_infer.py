"""Design-time: turn an unfamiliar EDC export into a reviewable profile.

This is the highest-value, lowest-risk use of a model in this pipeline. Claude
looks at a *sample* of an unknown export once and proposes a
`*.profile.json` — a declarative description of where forms, fields and visits
live in that format. A human reads the profile (it is a page of JSON), fixes
anything wrong, and commits it.

From then on the model is out of the loop entirely: parsing is the
deterministic `sources.profile` adapter. The AI produced *configuration*, not
data, and the configuration is versioned, diffable and testable — which is
what a validated pipeline needs.

    python -m crf_annotation.ai.schema_infer vendor_export.json -o profiles/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import pandas as pd

from ..design import DesignError
from ..logging_utils import configure_logging, get_logger
from ..sources.profile import Profile, ProfileSource
from .client import AiSettings, ClaudeClient

logger = get_logger()

PROMPT_VERSION = "1"
MAX_SAMPLE_ROWS = 8
MAX_SAMPLE_CHARS = 60000

SYSTEM_PROMPT = """\
You write parser profiles for clinical trial EDC study-design exports.

Given a sample of an export, identify where these live and describe them as a \
profile: forms (CRF page definitions), fields (items captured on a form), \
visits (study events/folders), and visit_forms (which forms are collected at \
which visit).

Requirements:
- Use only keys, sheet names and column headers that appear in the sample. \
Never invent one.
- fields.oid must be the short field name that a mapping specification would \
reference, not a long prefixed OID, when both are present.
- fields.label must be the text a human sees printed on the CRF page — the \
question or prompt, not the internal name.
- Omit the visits and visit_forms sections entirely if the export does not \
contain them. A profile without visits is valid.
- match.filename_contains and match.requires_keys should be distinctive enough \
that this profile does not claim files from a different vendor.
- notes: state what you were unsure about, or "" if nothing."""

RESPONSE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "format": {"type": "string", "enum": ["json", "tabular"]},
        "confidence": {"type": "number"},
        "notes": {"type": "string"},
        "match": {
            "type": "object",
            "properties": {
                "filename_contains": {"type": "array", "items": {"type": "string"}},
                "requires_keys": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["filename_contains", "requires_keys"],
            "additionalProperties": False,
        },
        "forms": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}, "sheet": {"type": "string"},
                "oid": {"type": "string"}, "label": {"type": "string"},
                "order": {"type": "string"},
            },
            "required": ["oid", "label"],
            "additionalProperties": False,
        },
        "fields": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}, "sheet": {"type": "string"},
                "oid": {"type": "string"}, "form_oid": {"type": "string"},
                "label": {"type": "string"}, "order": {"type": "string"},
            },
            "required": ["oid", "form_oid", "label"],
            "additionalProperties": False,
        },
        "visits": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}, "sheet": {"type": "string"},
                "oid": {"type": "string"}, "label": {"type": "string"},
                "order": {"type": "string"},
            },
            "required": ["oid", "label"],
            "additionalProperties": False,
        },
        "visit_forms": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}, "sheet": {"type": "string"},
                "visit_oid": {"type": "string"}, "form_oid": {"type": "string"},
            },
            "required": ["visit_oid", "form_oid"],
            "additionalProperties": False,
        },
    },
    "required": ["name", "format", "confidence", "notes", "match", "forms", "fields"],
    "additionalProperties": False,
}


def sample_export(path: Path) -> tuple[str, str]:
    """Return (format, a compact textual sample) for an unknown export."""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return "tabular", _sample_workbook(path)
    if suffix == ".csv":
        frame = pd.read_csv(path, nrows=MAX_SAMPLE_ROWS)
        return "tabular", f"# CSV: {path.name}\n{frame.to_csv(index=False)}"
    if suffix == ".json":
        return "json", _sample_json(path)
    raise DesignError(f"Cannot sample {path.name}: only .xlsx/.xls/.csv/.json are supported")


def _sample_workbook(path: Path) -> str:
    chunks: list[str] = []
    with pd.ExcelFile(path) as book:
        chunks.append(f"# Workbook: {path.name}\n# Sheets: {', '.join(book.sheet_names)}\n")
        for sheet in book.sheet_names[:15]:
            frame = book.parse(sheet, nrows=MAX_SAMPLE_ROWS)
            chunks.append(f"\n## Sheet '{sheet}' ({len(frame.columns)} columns)\n")
            chunks.append(frame.to_csv(index=False))
    return "".join(chunks)[:MAX_SAMPLE_CHARS]


def _sample_json(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))

    def shrink(node: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "..."
        if isinstance(node, Mapping):
            return {k: shrink(v, depth + 1) for k, v in list(node.items())[:40]}
        if isinstance(node, list):
            return [shrink(item, depth + 1) for item in node[:3]] + (
                [f"...{len(node) - 3} more"] if len(node) > 3 else []
            )
        return node

    return f"# JSON: {path.name}\n{json.dumps(shrink(data), indent=2)}"[:MAX_SAMPLE_CHARS]


def infer_profile(
    path: Path,
    settings: Optional[AiSettings] = None,
    client: Optional[ClaudeClient] = None,
) -> tuple[Profile, dict[str, Any]]:
    """Ask Claude to describe an unknown export. Returns (profile, raw answer)."""
    path = Path(path)
    fmt, sample = sample_export(path)

    settings = settings or AiSettings(enabled=True)
    settings.prompt_version = PROMPT_VERSION
    client = client or ClaudeClient(settings)

    payload = client.json_call(
        system=SYSTEM_PROMPT,
        user=(
            f"This export is in {fmt} form. Propose a parser profile for it.\n\n"
            f"{sample}"
        ),
        schema=RESPONSE_SCHEMA,
        cache_salt=path.name,
    )
    if not payload:
        raise DesignError(f"Could not infer a profile for {path.name}")

    spec = {k: v for k, v in payload.items() if k not in {"confidence", "notes"}}
    profile = Profile.from_dict(spec)

    logger.info("Proposed profile '%s' (model confidence %.2f)",
                profile.name, float(payload.get("confidence", 0)))
    if payload.get("notes"):
        logger.warning("Model notes: %s", payload["notes"])
    return profile, payload


def verify_profile(profile: Profile, path: Path) -> str:
    """Parse the export with the proposed profile and report what came out.

    Always run this before trusting a generated profile — it turns "the model
    said so" into "it read 42 forms and 900 fields, and here are three of them".
    """
    design = ProfileSource(profile).load(Path(path))
    design.validate()
    preview = design.field_frame.head(5).to_string(index=False)
    return (
        f"{len(design.forms)} forms, {len(design.fields)} fields, "
        f"{len(design.visits)} visits, {len(design.visit_forms)} visit/form pairs\n\n"
        f"First fields parsed:\n{preview}"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crf_annotation.ai.schema_infer",
        description="Propose a study-design parser profile for an unknown EDC export.",
    )
    parser.add_argument("export", type=Path, help="The vendor export to inspect")
    parser.add_argument("-o", "--out-dir", type=Path, default=Path("."),
                        help="Where to write <name>.profile.json")
    parser.add_argument("--model", default=None, help="Override the Claude model")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip parsing the export with the proposed profile")
    args = parser.parse_args(argv)

    configure_logging(None)
    settings = AiSettings.from_env(model=args.model)
    settings.enabled = True

    profile, raw = infer_profile(args.export, settings)

    slug = "".join(c if c.isalnum() else "_" for c in profile.name.lower()).strip("_")
    out_path = Path(args.out_dir) / f"{slug}.profile.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(profile.spec, indent=2) + "\n", encoding="utf-8")

    print(f"\nProposed profile written to {out_path}\n")
    print(json.dumps(profile.spec, indent=2))
    if not args.no_verify:
        print("\nVerification — parsed with the proposed profile:\n")
        try:
            print(verify_profile(profile, args.export))
        except DesignError as exc:
            print(f"  FAILED: {exc}")
            print("\n  The profile needs correcting before use.")
            return 1
    print("\nReview this profile, correct anything wrong, and commit it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
