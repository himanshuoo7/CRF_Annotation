#!/usr/bin/env python3
"""Explain why an aCRF has fewer annotations than expected.

Reads the config workbook a run produced and reports, in the order these
problems actually occur:

  1. pages bound to no form        - the CRF's form names do not match the build
  2. forms with no annotations     - no mapping spec covers that form
  3. unmapped variables            - the form is mapped, these fields are not
  4. low-confidence matches        - guessed, and awaiting review

    diagnose.py OUTPUT_DIR_OR_WORKBOOK [--json]
"""

from __future__ import annotations

import argparse
import sys

from _common import emit, find_workbook, read_coordinates, read_other_sheets

LOW_CONFIDENCE = 0.85


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="Output directory or aCRF_Config.xlsx")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    workbook = find_workbook(args.target)
    coordinates = read_coordinates(workbook)
    others = read_other_sheets(workbook)

    forms = coordinates[coordinates["ANNOTATION_TYPE"] == "Form"]
    fields = coordinates[coordinates["ANNOTATION_TYPE"] == "Field"]
    annotated_pages = set(fields.loc[fields["A_TEXT"] != "", "PAGE_NO"])
    all_pages = set(coordinates["PAGE_NO"])

    unbound = sorted(all_pages - set(forms["PAGE_NO"]))
    bare_pages = sorted(all_pages - annotated_pages)

    per_form = {}
    for _, row in forms.iterrows():
        oid = str(row.get("DS_OID") or "")
        name = str(row.get("DS_NAME") or oid)
        page = int(row["PAGE_NO"])
        count = int(((fields["PAGE_NO"] == page) & (fields["A_TEXT"] != "")).sum())
        entry = per_form.setdefault(oid, {"form": name, "pages": [], "annotations": 0})
        entry["pages"].append(page)
        entry["annotations"] += count
    forms_without_annotations = [v for v in per_form.values() if v["annotations"] == 0]

    methods = fields.loc[fields["A_TEXT"] != "", "MATCH_METHOD"].replace("", "unknown")
    low = fields[(fields["A_TEXT"] != "") &
                 (fields["MATCH_CONFIDENCE"].fillna(0) > 0) &
                 (fields["MATCH_CONFIDENCE"].fillna(0) < LOW_CONFIDENCE)]

    unmapped = others.get("unmap")
    unmapped_rows = [] if unmapped is None or unmapped.empty else \
        unmapped.head(50).to_dict("records")

    report = {
        "workbook": str(workbook),
        "pages_total": len(all_pages),
        "pages_annotated": len(annotated_pages),
        "pages_not_submitted": bare_pages,
        "pages_with_no_form_binding": unbound,
        "forms_seen": len(per_form),
        "forms_without_annotations": forms_without_annotations,
        "annotations": int((fields["A_TEXT"] != "").sum()),
        "match_methods": {k: int(v) for k, v in methods.value_counts().items()},
        "low_confidence": [
            {"row": int(i), "page": int(r["PAGE_NO"]), "crf_text": str(r["ITM_TEXT"]),
             "annotation": str(r["A_TEXT"]), "confidence": round(float(r["MATCH_CONFIDENCE"]), 3)}
            for i, r in low.iterrows()
        ],
        "unmapped_variables": unmapped_rows,
    }

    if args.json:
        emit(report, True)
        return 0

    print(f"{workbook}\n")
    print(f"  {report['annotations']} annotation(s) across "
          f"{report['pages_annotated']} of {report['pages_total']} pages")
    if report["match_methods"]:
        print("  by method: " + ", ".join(f"{k}={v}" for k, v in
                                          sorted(report["match_methods"].items())))
    print()

    if unbound:
        print(f"! {len(unbound)} page(s) bound to no form: {_short(unbound)}")
        print("    The form names printed on the CRF do not match the study design.")
        print("    Check the binder in the run log, or set binding.banner_prefix.\n")

    if forms_without_annotations:
        print(f"! {len(forms_without_annotations)} form(s) produced no annotations:")
        for entry in forms_without_annotations[:10]:
            print(f"    {entry['form']} (page {_short(entry['pages'])})")
        print("    Usually means no mapping spec targets that form, or the spec's")
        print("    source_dataset does not match the form OID.\n")

    if bare_pages:
        print(f"  {len(bare_pages)} page(s) marked NOT SUBMITTED: {_short(bare_pages)}")
        print("    Expected for pages that genuinely carry no submitted data.\n")

    if report["low_confidence"]:
        print(f"! {len(report['low_confidence'])} annotation(s) below "
              f"{LOW_CONFIDENCE:.2f} confidence — review these first:")
        for entry in report["low_confidence"][:10]:
            print(f"    p{entry['page']:<4} {entry['crf_text'][:34]:<34} "
                  f"-> {entry['annotation'][:26]:<26} ({entry['confidence']:.2f})")
        print("    Run the review command to resolve them.\n")

    if unmapped_rows:
        print(f"  {len(unmapped_rows)} unmapped variable(s) recorded on the 'unmap' sheet.")
        print("    These fields exist on a mapped form but no spec maps them.")

    if not (unbound or forms_without_annotations or report["low_confidence"]):
        print("  No structural problems found.")
    return 0


def _short(values, limit: int = 12) -> str:
    shown = ", ".join(str(v) for v in values[:limit])
    return shown + (f" (+{len(values) - limit} more)" if len(values) > limit else "")


if __name__ == "__main__":
    sys.exit(main())
