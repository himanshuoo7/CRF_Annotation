# CRF_Annotation

SDTM annotated CRF (aCRF) generation: take a blank case report form and label
each field with the SDTM variable it maps to, then produce the annotated PDF,
its bookmarks, and a reviewable configuration workbook.

## Layout

| Path | What it is |
|---|---|
| **`optimized_crf/`** | The `crf-annotation` Python package — the working implementation |
| **`skill/acrf/`** | A portable agent skill: `SKILL.md` plus review and diagnose scripts |
| **`acrf-plugin/`** | Claude Code packaging around that skill |

## Quick start

```bash
cd optimized_crf
pip install -e ".[dev]"

crf-annotate --crf blank_crf.pdf --specs specs/ --design study_design.xml \
             --out out/ --study STUDY123 --json
```

Or as a library:

```python
from crf_annotation import annotate_crf

result = annotate_crf(crf="blank_crf.pdf", specs="specs/",
                      design="study_design.xml", output_dir="out/",
                      study="STUDY123")
print(result.acrf, result.needs_review)
```

Replay a reviewed workbook, corrections and all:

```bash
crf-annotate --crf blank_crf.pdf --config out/aCRF_Config.xlsx --out out/ --study STUDY123
```

## What it supports

Study design is read from any EDC via pluggable adapters: Medidata Rave (ALS
workbook or `source_metadata` JSON), **CDISC ODM-XML** — which covers Veeva
Vault CDMS, Medrio, OpenClinica, Castor and Viedoc — or any other format via a
declarative `*.profile.json` with no code.

CRF text is matched to EDC fields in tiers: exact, then deterministic fuzzy,
then optionally a language model for the residue. Every non-exact match is
recorded in the config workbook with its confidence and reason, for a human to
accept, correct or reject. Reruns replay those decisions deterministically.

## Documentation

- [`optimized_crf/README.md`](optimized_crf/README.md) — architecture, production notes, configuration
- [`optimized_crf/DESIGN.md`](optimized_crf/DESIGN.md) — why the pipeline is built this way
- [`optimized_crf/EDC-SUPPORT.md`](optimized_crf/EDC-SUPPORT.md) — adding an EDC; where AI assist fits and where it does not
- [`optimized_crf/CHANGELOG.md`](optimized_crf/CHANGELOG.md) — release notes and known limitations

## Tests

```bash
cd optimized_crf && pytest
```

73 tests. The suite builds a synthetic study — CRF PDF, EDC metadata, specs,
visit matrix — and runs the whole pipeline, including a rerun with an edited
annotation. No real study data is used or committed.

## Status

Verified against synthetic fixtures and one live model call. **Not yet run
against a real study.** See *Known limitations* in
[`optimized_crf/CHANGELOG.md`](optimized_crf/CHANGELOG.md) before relying on it
for a submission deliverable.
