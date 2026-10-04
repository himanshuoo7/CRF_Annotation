---
name: acrf-run
description: Generate an SDTM annotated CRF from a blank CRF, mapping specs and an EDC study design export.
---

Follow the **Generating an aCRF** section of the `acrf` skill.

Collect the three inputs first — blank CRF PDF, mapping specs, study design
export — and ask for any the user has not given rather than guessing at paths.

Run with `--json` and report against `fields`, not in isolation: a large gap
between `annotations` and `fields` means something upstream failed, and you
should diagnose rather than report success. Surface `needs_review` if non-zero
and offer `/acrf:acrf-review`.

$ARGUMENTS
