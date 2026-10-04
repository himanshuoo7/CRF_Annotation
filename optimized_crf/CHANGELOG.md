# Changelog

## 1.0.0

First release.

### Core

- Annotate a blank CRF PDF with SDTM mappings, producing `acrf.pdf`, its
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

### FDA conformance (Study Data Technical Conformance Guide §4.1.4.6)

- The annotated PDF is named `acrf.pdf`, exactly as the guide specifies.
- Fields that are collected on the CRF but carry no SDTM mapping are annotated
  with the text `NOT SUBMITTED`, **per field**, beside the field itself. The
  guide requires this wherever data are recorded but not submitted; a
  page-level banner only covers pages with no collected fields at all.
  Disable with `matching.mark_not_submitted` / `--no-mark-not-submitted`.
- The annotation text is `NOT SUBMITTED`, unbracketed, as quoted in the guide.
- `NOT SUBMITTED` marks are counted separately from `needs_review`: they are a
  deliberate statement, not a guessed match. The `unmap` sheet still lists the
  variables, so the mapping gap stays visible.

Remember the guide also expects an explanation in the Reviewer's Guide of why
those data were not submitted. That document is outside this tool.

### Conventions

- Derived and assigned SDTM variables are not annotated. Exact names
  (`USUBJID`, `STUDYID`, `EPOCH`, ...) and the `--` domain-prefix convention
  (`--SEQ` covering `AESEQ`, `VSSEQ`, ...) are recognised and dropped from the
  catalogue, with the dropped targets logged. CDISC documents those in
  define.xml with an origin of Derived or Assigned instead. Override with
  `matching.include_derived` or `--include-derived`.
- Bookmark destinations use Inherit Zoom (`/XYZ` with null left, top and
  zoom), as the FDA Study Data Technical Conformance Guide asks for, so
  following a bookmark preserves the reader's magnification.

### Known limitations

- **Not yet run against a real study.** Verified against synthetic fixtures and
  one live model call.
- The Reviewer's Guide and define.xml page linkage are out of scope.
- Bookmark structure and Inherit Zoom follow the FDA eCTD PDF specification,
  which is referenced from SDTCG footnote 51 but was not read directly when
  this was implemented. Verify against that document before a submission.
- Scanned CRFs with no text layer are not supported.
