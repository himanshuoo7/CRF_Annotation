---
name: acrf
description: Generate, review and diagnose SDTM annotated CRFs (aCRFs) from a blank CRF PDF, SDTM mapping specs and an EDC study design export (Medidata Rave, CDISC ODM-XML from Veeva/Medrio/OpenClinica/Castor/Viedoc, or any format via a profile). Use when asked to annotate a CRF, produce or fix an aCRF, review annotation matches, or explain why annotations are missing.
---

# SDTM annotated CRF generation

An **annotated CRF** is the blank case report form with each field labelled by
the SDTM variable it maps to — `DM.BRTHDTC`, `AE.AETERM`,
`SUPPAE.QVAL where QNAM = AESEV`. It is a regulatory submission deliverable:
reviewers use it to trace a value in a dataset back to the question that
captured it.

Two consequences shape everything below:

- **A wrong annotation is worse than a missing one.** A gap is caught in
  review; a confident mistake may ship.
- **Never edit the PDF.** The config workbook is the source of truth. Correct
  the workbook, then rerun from it.

## Setup

Requires the `crf-annotation` package: `pip install crf-annotation`. Check with
`crf-annotate --help`. If it is a source checkout instead, run the commands as
`python -m crf_annotation` from that directory.

## Generating an aCRF

You need three things. Ask for whichever is missing rather than guessing:

| Input | What it is |
|---|---|
| **Blank CRF** | The unannotated CRF PDF. Must have a text layer — a scan cannot be annotated. |
| **Mapping specs** | JSON files describing raw field → SDTM variable mappings. |
| **Study design** | The EDC's export of forms, fields and visits. |

```bash
crf-annotate --crf blank_crf.pdf --specs specs/ --design study_design.xml \
             --out output/ --study STUDY123 --json
```

`--specs` and `--design` each accept a file, a directory, or several paths.
Always pass `--json` — parse the result rather than scraping the log.

Read the result before reporting success:

- `annotations` against `fields` — a large gap means something upstream failed;
  diagnose rather than declaring victory.
- `needs_review` — annotations that were **not** exact matches. Offer to review.
- `annotations_dropped` — no free space on the page.
- `study_design_source` — confirms which adapter read the design. If this is not
  what the user expected, the wrong file was picked up.

Exit status: `0` success, `1` the run failed, `2` bad inputs.

## Reviewing non-exact matches

This is the part that needs a human, and it is where you are most useful.

```bash
python scripts/review.py output/ --json
```

Present the pending rows lowest-confidence first: page, CRF text, proposed
annotation, confidence, and the recorded reason.

Your job is to give the user something to react to, **not to decide for them**:

- Where the match is a wording difference you can see (`DOB` → `Date of Birth`,
  a plural, a wrapped label), say so and why.
- Where the distinction is clinical rather than textual — **race vs ethnicity**,
  a start date vs a collection date, severity vs seriousness — say it needs
  their judgement and do not guess. These are exactly the cases a model gets
  plausibly, confidently wrong.

Then write their decisions and apply:

```json
[{"row": 12, "annotation": "DM.SEX"},
 {"row": 14, "reject": true, "note": "section heading, not a field"}]
```

```bash
python scripts/review.py output/aCRF_Config.xlsx --apply decisions.json
crf-annotate --crf blank_crf.pdf --config output/aCRF_Config.xlsx --out output/ --study STUDY123
```

Applied rows are marked `reviewed` at confidence 1.0. **Nothing is redrawn
until the rerun** — say so explicitly.

## Diagnosing a thin result

```bash
python scripts/diagnose.py output/ --json
```

Work the causes in pipeline order; an early one explains everything after it.

| Symptom | Cause |
|---|---|
| Every page NOT SUBMITTED | Form binding failed — the CRF's form names do not match the design |
| One domain missing entirely | No spec targets that form, or its `source_dataset` ≠ the form OID |
| Annotations on the wrong form | Dataset scope fell back to variable-only matching; the log says so |
| Fields skipped on a busy page | No free space; see `annotations_dropped` |
| Nothing extracted at all | Scanned PDF with no text layer; OCR is out of scope |

Pages that genuinely carry no submitted data are *supposed* to say NOT
SUBMITTED. Do not treat every blank page as a fault.

## Matching tiers

1. `exact` — normalised string equality. No review needed.
2. `fuzzy` — deterministic near-match. Catches plurals, spacing, punctuation.
3. `claude` — only the residue, only from that form's field list, cached so a
   rerun makes no calls. Disable with `--no-ai`.

`--exact-only` gives a fully deterministic run for validation.

## Rules

- Do not raise `--min-confidence` to make `needs_review` go away. That hides
  guesses instead of resolving them.
- Do not invent annotations the mapping specs do not support. The spec is the
  controlled document.
- Do not re-run with different settings to make a failure pass without telling
  the user what you changed and why.

## More detail

- `reference/workbook.md` — the config workbook column contract and coordinates
- `reference/edc-support.md` — supported EDC exports and adding a new one
