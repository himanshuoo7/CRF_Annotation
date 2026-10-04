# crf_annotation (optimized)

SDTM aCRF generation: take a blank CRF PDF plus the study's EDC metadata and
mapping specs, and produce an annotated PDF with bookmarks and a reviewable
config workbook.

Callable as a library, as a CLI, or as an agent skill.

```python
from crf_annotation import annotate_crf

result = annotate_crf(
    crf="blank_crf.pdf",
    specs="specs/",                 # file, directory, or list
    design="study_design.xml",      # Rave ALS, CDISC ODM-XML, ...
    output_dir="out/",
    study="STUDY123",
)
print(result.acrf, result.needs_review)
```

```bash
crf-annotate --crf blank_crf.pdf --specs specs/ --design study_design.xml \
             --out out/ --study STUDY123 --json
```

Replay a reviewed workbook, corrections and all:

```bash
crf-annotate --crf blank_crf.pdf --config out/aCRF_Config.xlsx --out out/ --study STUDY123
```

Exit status is `0` on success, `1` on a failed run, `2` on bad inputs. The
Python API raises `InputError` / `PipelineError` instead.

## Install

```bash
pip install -r requirements.txt     # or: pip install -e ".[dev]"
```

## Supported EDC systems

Medidata Rave (ALS workbook or `source_metadata_*.json`), **Veeva Vault CDMS
and any other system that exports CDISC ODM-XML**, and any other format at all
via a declarative `*.profile.json`. See **[EDC-SUPPORT.md](EDC-SUPPORT.md)**.

## How a run works

```
inputs ──► CrfJob ──► first run? ──► specs + EDC study design ──┐
                 │                                              ├──► coordinate table
                 └──► rerun? ─────► config workbook ────────────┘        │
                                                                         ▼
                                           bookmarks (outline) ──► annotations ──► publish
```

Two modes, decided once by `CrfPayload`:

| Mode | Trigger | Source of annotations |
|------|---------|-----------------------|
| First run | `specs=` and `design=` given | Spec JSONs + the EDC study design + the CRF's own text |
| Rerun | `config=` points at an `aCRF_Config.xlsx` | The workbook, including any edits a reviewer made |

A rerun replays whatever is in the workbook, so hand-corrected annotation text
survives — that is the point of the workbook.

## Modules

| Module | Responsibility |
|--------|----------------|
| `job.py` | Validate inputs once; decide the run mode |
| `workspace.py` | Temp directory (context-managed) and the publish step |
| `design.py` | `StudyDesign` — the canonical, EDC-agnostic study model |
| `sources/` | One adapter per EDC export format, plus the sniffing registry |
| `binding.py` | Which form each CRF page belongs to (per-vendor strategies) |
| `specs.py` | Mapping spec JSONs → `(dataset, variable) → annotation text` |
| `pdf_text.py` | Text and geometry extraction from the blank CRF |
| `merge.py` | CRF text → EDC field → SDTM annotation joins |
| `matching.py` | Tiered matching: exact → fuzzy → optional resolver |
| `ai/` | Optional Claude assist: schema inference, label matching |
| `layout.py` | Collision-free placement of annotation boxes |
| `config_workbook.py` | Read/write the `aCRF_Config.xlsx` contract |
| `annotate.py` | Draw the boxes onto the PDF |
| `bookmarks.py` | Study → Domains/Visits → forms outline |
| `pipeline.py` | Orchestration; `annotate_crf` |

## Coordinates

Everything outside `annotate.py` uses **PDF user space: origin bottom-left**,
serialised as `[x0, y0, x1, y1]` — the same convention the config workbook's
`LEFT_BOTTOM_RIGHT_TOP` column has always used. `annotate.py` is the only place
that flips to PyMuPDF's top-left origin, and it detects header rectangles
written by older versions (which were stored top-left) so old workbooks still
render correctly.

## Dependencies and licensing

| Package | Licence |
|---|---|
| pandas, XlsxWriter, openpyxl | BSD-3 / MIT |
| pypdf | BSD-3 |
| anthropic | MIT |
| **PyMuPDF** | **AGPL-3.0, or a commercial licence from Artifex** |

PyMuPDF is the only copyleft dependency. AGPL obligations attach on
distribution and on network use, so this matters before the package is shipped
or offered as a service, not while it is used internally.

A permissive substitute exists if that becomes a problem: `pdfplumber` (MIT)
for text extraction and `pypdf` (BSD-3) for annotations, which together cover
everything PyMuPDF is used for here. The cost is roughly 6x slower extraction —
about four seconds on a 300-page CRF — and a rewrite of `pdf_text.py` and
`annotate.py`.

## Tests

```bash
pytest
```

Verified on pandas 2.1 / Python 3.11 and pandas 3.0 / Python 3.12.

> **Install note:** the Anthropic SDK 1.x uses `httpx2`. In a virtualenv created
> with `--system-site-packages` over an Anaconda base, it picks up Anaconda's
> older `httpcore`/`h11` and every request fails with a misleading
> `APIConnectionError: Connection error` (the real cause is
> `TypeError: process() takes no keyword arguments`). Use a clean venv.

The suite builds a synthetic 3-page study — CRF PDF, EDC metadata, specs and an
ALS workbook — and runs the whole pipeline, including a rerun with an edited
annotation.

## Compatibility notes

- Coordinates from the workbook are parsed explicitly, never evaluated — that
  cell is user-editable.
- Annotation drawing probes the PyMuPDF API once and adapts to it; the
  `border_color` and `update(text_align=…)` call shapes changed at 1.25.
- A run that cannot produce a correct aCRF raises rather than returning a
  successful-looking result with an empty PDF.

## Running in production

**Concurrency.** A run takes an advisory lock on its target directory and
refuses to start if another holds it, rather than interleaving two runs'
output. Errors as `ConcurrentRunError`.

**Atomic publish.** Artefacts are staged and renamed into place, so a consumer
never sees a half-written aCRF. A crash mid-publish leaves the previous
version intact.

**Logs** rotate at 20 MB, five generations. A 300-page study writes ~30 lines.

**Result.** `AnnotationResult`, or `--json` on the CLI:

```json
{
  "status": "ok",
  "acrf": "/…/aCRF.pdf",
  "config": "/…/aCRF_Config.xlsx",
  "study": "STUDY123",
  "mode": "first_run",
  "study_design_source": "CDISC ODM-XML",
  "pages": 42, "forms": 18, "fields": 640,
  "annotations": 601, "annotations_dropped": 0,
  "match_methods": {"exact": 574, "fuzzy": 19, "claude": 8},
  "needs_review": 27,
  "unmapped_variables": 12
}
```

`needs_review` is the number worth gating on: it counts annotations that did
not come from an exact match.

**Configuration** (all optional):

```python
from crf_annotation import BindingOptions, MatchingOptions, annotate_crf

annotate_crf(
    ...,
    matching=MatchingOptions(
        ai=True,                 # False for deterministic-only
        exact_only=False,        # True also disables the fuzzy tier
        min_confidence=0.75,
        fuzzy_threshold=0.88,
        dataset_scope="auto",    # auto | strict | off
        model="claude-opus-5",
        cache_dir="/shared/acrf-ai-cache",
    ),
    binding=BindingOptions(
        banner_prefix="Form: ",
        allow_sequential_fallback=False,   # guesses; needs full review if used
    ),
)
```

Every option has a CLI flag too (`--no-ai`, `--exact-only`, `--dataset-scope`,
`--banner-prefix`, …).

**Validation runs.** `--exact-only` gives a fully deterministic run with no
fuzzy or AI tier, for reproducing a historical output.

---

## Using it from an agent

The portable skill lives in [`../skill/acrf`](../skill/acrf) — a `SKILL.md`
plus review/diagnose scripts, usable by any agent that loads skills. Claude
Code packaging is in [`../acrf-plugin`](../acrf-plugin).

---

See `DESIGN.md` for why the pipeline is built this way, `EDC-SUPPORT.md` for
adding a new EDC and where AI assist fits, and `CHANGELOG.md` for release notes.
