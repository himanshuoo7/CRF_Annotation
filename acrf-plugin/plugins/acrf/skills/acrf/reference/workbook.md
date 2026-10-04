# The config workbook

`aCRF_Config.xlsx` is the record of what was annotated and why. It is the
**only** supported way to correct a run: edit it, then rerun with `--config`.

## Sheet `coordinates`

One row per thing drawn, plus one per form for page↔form identity.

| Column | Meaning |
|---|---|
| `PAGE_NO(REF)` | 1-based page number |
| `SOURCE_DATASET(REF)` | EDC form OID |
| `CRF_TEXT(REF)` | text found on the page |
| `FORM_NAME(REF)` | form label |
| `LEFT_BOTTOM_RIGHT_TOP` | `[x0, y0, x1, y1]` |
| `ANNOTATION` | the SDTM annotation drawn — **this is the editable one** |
| `ANNOTATION_TYPE_HEADER_OR_FIELD(REF)` | `Form`, `Header` or `Field` |
| `MATCH_METHOD(REF)` | `exact`, `fuzzy`, `claude`, `reviewed` |
| `MATCH_CONFIDENCE(REF)` | 0–1 |
| `MATCH_NOTE(REF)` | why the match was made |

Columns ending `(REF)` are reference: they describe what was found, and
changing them does not change the output. `ANNOTATION` is what gets drawn.

### Annotation types

- **`Form`** — carries page↔form identity, draws nothing. Present even for pages
  with no annotations, so the form→page map stays complete for bookmarking.
- **`Header`** — the domain banner at the top of a page (`AE (Adverse Events)`).
- **`Field`** — the annotations themselves.

### Correcting a run

Change `ANNOTATION` and rerun. To remove an annotation, blank the cell. To add
one, set it on the row for that CRF text. Then:

```bash
crf-annotate --crf blank.pdf --config aCRF_Config.xlsx --out output/ --study STUDY123
```

Pass `--study` on a rerun too: the workbook does not store the study name, and
without it the PDF title falls back to the CRF filename.

## Sheet `unmap`

Fields that exist on a mapped form but that no spec maps. A spec gap, not a
tool fault.

## Sheet `FileNames`

Provenance: which study-design adapter read the design, and which files fed
the run.

## Coordinates

PDF user space, **origin bottom-left**, serialised `[x0, y0, x1, y1]` — which
is what `LEFT_BOTTOM_RIGHT_TOP` means. Page size is read from the PDF, never
assumed, so A4 and landscape work.

Only the renderer flips to the top-left origin PDF libraries use. Header
rectangles written by older versions were stored top-left; they are detected
and passed through, so old workbooks still render.
