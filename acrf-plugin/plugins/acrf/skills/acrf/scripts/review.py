#!/usr/bin/env python3
"""List — and optionally resolve — annotations that were not an exact match.

Exact matches need no review. Everything else (fuzzy, Claude) is a judgement
call that a human owns. This surfaces exactly that set, worst confidence first.

    review.py OUTPUT_DIR_OR_WORKBOOK [--json] [--all] [--apply decisions.json]

`--apply` takes a JSON list of decisions and writes them back:

    [{"row": 12, "annotation": "DM.SEX"},     # correct it
     {"row": 14, "reject": true}]             # not a field after all

Applied rows are marked `reviewed` with confidence 1.0, so a rerun replays the
human decision and never consults a model about that row again.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import (
    die,
    emit,
    find_workbook,
    read_coordinates,
    read_other_sheets,
    write_workbook,
)

REVIEWED = "reviewed"
NO_REVIEW_NEEDED = {"", "exact", REVIEWED}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="Output directory or aCRF_Config.xlsx")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--all", action="store_true",
                        help="Include rows already reviewed")
    parser.add_argument("--apply", metavar="FILE",
                        help="JSON decisions file to write back into the workbook")
    args = parser.parse_args(argv)

    workbook = find_workbook(args.target)
    coordinates = read_coordinates(workbook)

    if args.apply:
        return _apply(workbook, coordinates, Path(args.apply), args.json)

    fields = coordinates[coordinates["ANNOTATION_TYPE"] == "Field"]
    pending = fields if args.all else fields[~fields["MATCH_METHOD"].isin(NO_REVIEW_NEEDED)]
    pending = pending.sort_values(["MATCH_CONFIDENCE", "PAGE_NO"])

    rows = [
        {
            "row": int(index),
            "page": int(row["PAGE_NO"]),
            "form": str(row.get("DS_NAME") or row.get("DS_OID") or ""),
            "crf_text": str(row["ITM_TEXT"]),
            "annotation": str(row["A_TEXT"]),
            "method": str(row["MATCH_METHOD"]),
            "confidence": round(float(row["MATCH_CONFIDENCE"] or 0), 3),
            "note": str(row.get("MATCH_NOTE") or ""),
        }
        for index, row in pending.iterrows()
    ]

    counts = fields["MATCH_METHOD"].replace("", "unmatched").value_counts().to_dict()
    if args.json:
        emit({"workbook": str(workbook), "counts": counts, "pending": rows}, True)
        return 0

    total = len(fields)
    print(f"{workbook}\n")
    print(f"{total} field annotation(s): " +
          ", ".join(f"{int(v)} {k}" for k, v in sorted(counts.items())))
    if not rows:
        reviewed = int(counts.get(REVIEWED, 0))
        if reviewed:
            print(f"\nNothing outstanding — {reviewed} annotation(s) already reviewed by hand.")
            print(f"Redraw with: crf-annotate --crf <BLANK.pdf> --config {workbook} "
                  f"--out {workbook.parent} --study <STUDY>")
        else:
            print("\nNothing needs review — every annotation was an exact match.")
        return 0

    print(f"\n{len(rows)} annotation(s) need review (lowest confidence first):\n")
    print(f"  {'row':>5}  {'pg':>3}  {'conf':>5}  {'method':<8} {'CRF text':<32} "
          f"{'annotation':<28} why")
    print("  " + "-" * 116)
    for r in rows:
        print(f"  {r['row']:>5}  {r['page']:>3}  {r['confidence']:>5.2f}  "
              f"{r['method']:<8} {r['crf_text'][:32]:<32} {r['annotation'][:28]:<28} {r['note']}")
    print("\nTo resolve: write a decisions file and run with --apply")
    print('  [{"row": 12, "annotation": "DM.SEX"}, {"row": 14, "reject": true}]')
    return 0


def _apply(workbook: Path, coordinates, decisions_path: Path, as_json: bool) -> int:
    if not decisions_path.is_file():
        die(f"decisions file not found: {decisions_path}")
    try:
        decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"decisions file is not valid JSON: {exc}")
    if not isinstance(decisions, list):
        die("decisions file must be a JSON list")

    applied, skipped = [], []
    for decision in decisions:
        if not isinstance(decision, dict) or "row" not in decision:
            skipped.append({"decision": decision, "reason": "missing 'row'"})
            continue
        row = int(decision["row"])
        if row not in coordinates.index:
            skipped.append({"row": row, "reason": "no such row in this workbook"})
            continue
        if decision.get("reject"):
            new_text = ""
        elif "annotation" in decision:
            new_text = str(decision["annotation"]).strip()
        else:
            skipped.append({"row": row, "reason": "needs 'annotation' or 'reject'"})
            continue

        before = str(coordinates.loc[row, "A_TEXT"])
        coordinates.loc[row, "A_TEXT"] = new_text
        coordinates.loc[row, "MATCH_METHOD"] = REVIEWED
        coordinates.loc[row, "MATCH_CONFIDENCE"] = 1.0
        coordinates.loc[row, "MATCH_NOTE"] = str(decision.get("note", "reviewed by hand"))[:120]
        applied.append({"row": row, "was": before, "now": new_text})

    if applied:
        write_workbook(workbook, coordinates, read_other_sheets(workbook))

    if as_json:
        emit({"applied": applied, "skipped": skipped, "workbook": str(workbook)}, True)
    else:
        for entry in applied:
            arrow = "cleared" if not entry["now"] else entry["now"]
            print(f"  row {entry['row']}: {entry['was'] or '(none)'} -> {arrow}")
        for entry in skipped:
            print(f"  skipped: {entry}", file=sys.stderr)
        print(f"\n{len(applied)} decision(s) written to {workbook.name}.")
        if applied:
            print("Redraw the PDF from your decisions:")
            print(f"  crf-annotate --crf <BLANK.pdf> --config {workbook} "
                  f"--out {workbook.parent} --study <STUDY>")
    return 0 if applied or not decisions else 1


if __name__ == "__main__":
    sys.exit(main())
