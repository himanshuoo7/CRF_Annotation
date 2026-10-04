# Changelog

## 1.0.0

First release.

### Core

- Annotate a blank CRF PDF with SDTM mappings, producing `aCRF.pdf`, its
  bookmark outline, and a reviewable `aCRF_Config.xlsx`.
- Two modes: derive annotations from mapping specs and an EDC study design, or
  replay a reviewed config workbook including a reviewer's corrections.
- Collision-aware annotation placement, bounds-checked against each page's real
  dimensions.
- Dual bookmark hierarchies — by SDTM domain and by visit.

### EDC support

- Canonical study-design model behind a sniffing adapter registry.
- Adapters for Medidata Rave (ALS workbook and `source_metadata` JSON) and
  CDISC ODM-XML, covering Veeva Vault CDMS, Medrio, OpenClinica, Castor and
  Viedoc.
- Declarative `*.profile.json` adapter for any other export format, no code.
- Page-to-form binding as a pluggable strategy rather than a fixed convention.

### Matching

- Tiered: exact, then deterministic fuzzy, then optionally a language model for
  the residue only.
- Annotations keyed on (source dataset, variable), with an automatic fallback
  to variable-only matching when a build's vocabularies do not line up.
- `MATCH_METHOD`, `MATCH_CONFIDENCE` and `MATCH_NOTE` recorded per annotation,
  so every non-exact match is visible to a reviewer.
- Model responses cached by content hash; reruns make no calls.
- `--exact-only` for fully deterministic validation runs.

### Interfaces

- Python API: `annotate_crf(...)` returning an `AnnotationResult`.
- `crf-annotate` CLI with `--json` for programmatic callers.
- A portable agent skill with review and diagnose scripts, plus Claude Code
  packaging around it.

### Operational

- Context-managed workspace, cleaned on every exit path.
- Atomic publish — a crash cannot leave a truncated deliverable.
- Advisory lock against concurrent runs into one output directory.
- Rotating logs; a 300-page study writes about 30 lines at `INFO`.

### Known limitations

- **Not yet run against a real study.** Verified against synthetic fixtures and
  one live model call.
- Derived and assigned SDTM variables (`USUBJID`, `--SEQ`, `EPOCH`, `--DY`) are
  not filtered out. `IGNORED_VARIABLES` exists in `constants.py` but is not
  wired in, so a spec that maps one would have it annotated — which CDISC
  conventions say it should not be.
- Bookmark destinations use `/Fit` rather than Inherit Zoom, which the FDA
  Study Data Technical Conformance Guide asks for.
- Producing the submission filename (`acrf.pdf`), the Reviewer's Guide, and
  define.xml page linkage are out of scope.
- Scanned CRFs with no text layer are not supported.
