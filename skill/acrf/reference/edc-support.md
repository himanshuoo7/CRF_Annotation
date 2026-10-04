# EDC support

Adapters translate any vendor export into one canonical study design — forms,
fields, visits, and which forms are collected at which visit. Nothing past the
adapter knows which system a study was built in.

## Supported today

| System | Route |
|---|---|
| Medidata Rave | ALS workbook (`Forms`/`Fields`/`Folders`/`Matrix#…`) |
| Medidata Rave | `source_metadata_*.json` |
| **Veeva Vault CDMS** | **CDISC ODM-XML export** |
| Medrio, OpenClinica, Castor, Viedoc | CDISC ODM-XML export |
| Anything else | `*.profile.json`, no code |

ODM is the CDISC interchange standard and most EDCs emit it, so one adapter
covers most of the market. Before building anything for a new vendor, **ask
whether they can export ODM.**

The right adapter is chosen by sniffing the file, so you normally just pass
`--design <file>` and let it work out what it is. The result's
`study_design_source` tells you which adapter won.

## Adding a format with no code

A profile says where each section lives:

```json
{
  "name": "Example CDMS export",
  "format": "json",
  "match": {"filename_contains": ["casebook"], "requires_keys": ["studyDesign"]},
  "forms":  {"path": "studyDesign.casebookForms", "oid": "formName",
             "label": "displayLabel", "order": "seq"},
  "fields": {"path": "studyDesign.items", "oid": "itemName",
             "form_oid": "parentForm", "label": "prompt"}
}
```

`format: "tabular"` does the same for spreadsheets and CSVs, with `"sheet"`
instead of `"path"`.

To generate one for an unknown export:

```bash
python -m crf_annotation.ai.schema_infer vendor_export.json -o profiles/
```

It samples the file, proposes a profile, then **parses the real export with it
and prints the counts**. Read the verification, not just the profile:

- Do the form and field counts match what the user expects?
- Are the parsed labels the text a human sees on the CRF, or internal names?
  If internal, the `label` key is wrong.
- Is `fields.oid` the short name the mapping specs reference?

Fix anything wrong by hand, re-verify, commit it. From then on parsing is
deterministic with no model involved.

## Page → form binding

Separate from the metadata, and just as vendor-specific: Rave prints a
`Form: <name>` banner on each page, others print the name in a header band.
Strategies are tried and whichever binds the most pages wins.

If a CRF uses an unusual banner, pass `--banner-prefix "Page: "`.

`--allow-sequential-fallback` assumes pages follow the design's form order. It
is a guess; every annotation from a run that used it needs review, and the log
says so.
