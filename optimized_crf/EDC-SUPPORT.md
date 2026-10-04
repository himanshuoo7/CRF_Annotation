# Supporting any EDC

The pipeline used to be written against one system: Medidata Rave. Two things
were hard-coded — the shape of the study-design export, and the `Form: ` banner
Rave prints on every blank-CRF page. Both are now strategies behind a seam.

```
  vendor export ──► adapter ──► StudyDesign ──► pipeline ──► aCRF
  (any format)      (sniffed)   (canonical)     (EDC-agnostic)
```

`StudyDesign` is the contract: **forms, fields, visits, visit→form pairs.**
Every EDC describes those four things; they disagree only on file format and
vocabulary. Nothing downstream of the adapter knows which system a study was
built in.

## What works today

| System | Route | Status |
|---|---|---|
| Medidata Rave | ALS workbook (`Forms`/`Fields`/`Folders`/`Matrix#…`) | Built in |
| Medidata Rave | `source_metadata_*.json` | Built in |
| **Veeva Vault CDMS** | **CDISC ODM-XML export** | **Built in** |
| Medrio, OpenClinica, Castor, Viedoc, … | CDISC ODM-XML export | Built in |
| Anything else | `*.profile.json` — no code | Built in |

ODM is the reason this is tractable. It is the CDISC interchange standard and
nearly every EDC can emit it, so one adapter covers most of the market. The
test suite runs the whole pipeline twice — once from Rave metadata, once from
an ODM export — and asserts the annotations come out identical.

## Adding a system with no code

Write a profile. It says which sheet or key holds each section:

```json
{
  "name": "Example Vault CDMS export",
  "format": "json",
  "match": {"filename_contains": ["casebook"], "requires_keys": ["studyDesign"]},
  "forms":  {"path": "studyDesign.casebookForms", "oid": "formName",
             "label": "displayLabel", "order": "seq"},
  "fields": {"path": "studyDesign.items", "oid": "itemName",
             "form_oid": "parentForm", "label": "prompt"}
}
```

Drop it in a directory, call `sources.load_profiles(directory)`, and that
format is supported. Profiles are data: reviewable, diffable, version
controlled — which is what a validated pipeline needs. `format: "tabular"`
does the same for spreadsheets and CSVs, with `"sheet"` instead of `"path"`.

Write a real adapter module (`sources/*.py`) only when the format needs logic a
profile cannot express — a matrix grid to melt, a reference graph to walk.

## Blank-CRF conventions

Which form a page belongs to is a separate problem from the metadata, and just
as vendor-specific. `binding.py` tries each strategy and keeps whichever binds
the most pages:

| Strategy | For |
|---|---|
| `BannerBinder` | Rave-style `Form: <name>` banners |
| `HeaderNameBinder` | A form name printed in the page header band |
| `SequentialBinder` | Opt-in last resort; logs loudly, everything needs review |

Pages nothing binds are left unannotated and listed in the log. They are never
guessed at.

---

# Where GenAI belongs — and where it does not

An aCRF is a regulated deliverable. Non-determinism in the output is a
liability, so the rule here is:

> **The model produces artefacts a human approves. It never produces the
> deliverable.**

Both AI features obey it. Label matching runs by default; schema inference is
a command you run deliberately.

## 1. Schema inference — design time

Point Claude at an unfamiliar export once. It proposes a **profile**, which a
human reads, corrects and commits.

```bash
python -m crf_annotation.ai.schema_infer vendor_export.json -o profiles/
```

It samples the file (sheet names, headers, a few rows), returns a profile under
a strict JSON schema, then **parses the real export with the proposed profile
and prints what came out** — 42 forms, 900 fields, and the first five, so you
are reviewing evidence rather than a claim.

After that the model is gone. Parsing is the deterministic profile adapter.
The AI produced *configuration*, not data. This is the highest-value, lowest-risk
use: it turns a week of reverse-engineering a vendor format into an afternoon
of review, and nothing it touched can vary between runs.

## 2. Label matching — run time, review-gated

Matching CRF text to EDC fields runs in tiers, cheapest first, each seeing only
what the last could not resolve:

| Tier | Method | Determinism |
|---|---|---|
| 1 | exact, normalised | total |
| 2 | fuzzy — `difflib` over sorted tokens | total |
| 3 | Claude | cached; on by default, `matching.ai: false` to disable |

Tier 3 exists because tiers 1–2 cannot read `DOB` as *Date of Birth*, or
recognise a label that wrapped across two lines, or a translated form. Those
are real annotation losses on real studies.

Constraints on the AI tier:

- It **chooses from the fields defined on that page's form**. A variable
  outside the candidate list is rejected in code, not just discouraged in the
  prompt — there is a test for it.
- It is instructed to return *no match* when two candidates are equally
  plausible. A missed annotation is caught in review; a wrong one may not be.
- Every answer carries a confidence and a one-line reason, and both land in the
  config workbook:

| CRF_TEXT(REF) | ANNOTATION | MATCH_METHOD | MATCH_CONFIDENCE | MATCH_NOTE |
|---|---|---|---|---|
| Sex | DM.SEX | exact | 1.000 | |
| Races | DM.RACE | fuzzy | 0.889 | token similarity 0.89 to 'Race' |
| DOB | DM.BRTHDTC | claude | 0.910 | common abbreviation for date of birth |

A reviewer filters on `MATCH_METHOD != exact` and sees the entire AI surface
area of the run on one screen.

- Responses are **cached under a hash of their exact input**, so the rerun path
  calls nothing. Reruns are reproducible and free.
- A failed or unavailable model is a warning, never a failed run. Reruns
  replay the workbook, so a study already reviewed never calls the model again.

## Determinism and audit

- `matching.exact_only` gives a fully deterministic run with no fuzzy or AI
  tier — use it for validation runs and for reproducing a historical output.
- The rerun path replays the config workbook. Once a human has signed off, the
  annotations are frozen data; no model is consulted a second time.
- Model id, effort and prompt version are hashed into the cache key, so
  changing any of them is a visible change rather than silent drift.
- Blank CRFs and study design metadata contain **no patient data** — this is
  build metadata, not clinical data.

## Model support

The request is shaped to the model. Adaptive thinking and `output_config.effort`
exist only on the 4.6-and-later families; sending either to Haiku 4.5 or
Sonnet 4.5 is a 400, so they are omitted for those models. Structured output
is sent to all of them.

| Model | Request shape | Rate (in/out per MTok) |
|---|---|---|
| `claude-opus-5` (default) | effort + adaptive thinking | $5 / $25 |
| `claude-sonnet-5` | effort + adaptive thinking | $3 / $15 |
| `claude-haiku-4-5` | structured output only | $1 / $5 |

**Verified live against Haiku 4.5.** A three-page CRF whose labels had drifted
from the build went from 2 exact matches to 7 annotations:

| CRF text | Matched | Conf. | Model's reason |
|---|---|---|---|
| DOB | DM.BRTHDTC | 0.95 | standard abbreviation for Date of Birth |
| Gender | DM.SEX | 0.85 | common synonym for Sex |
| Ethnic origin | DM.RACE | 0.80 | closely aligns with Race field |
| AE Term | AE.AETERM | 0.95 | standard abbreviation for Adverse Event Term |
| Onset | AE.AESTDTC | 0.80 | refers to start date of adverse event |
| "Please complete all fields in black ink" | — | — | correctly rejected as page furniture |
| "kg" | — | — | correctly rejected as a unit |

Two calls, 1,473 input / 248 output tokens, **$0.0027** for the study. The
immediate rerun made **zero calls** and produced identical output from cache.

Cost scales with the *unmatched residue*, not study size — a well-built study
where exact matching handles everything costs nothing at all, because the tier
never runs.

## Configuration

```python
payload["matching"] = {
    "ai": True,                 # default True
    "min_confidence": 0.75,     # below this, leave unmatched
    "fuzzy_threshold": 0.88,
    "model": "claude-opus-5",   # default
    "effort": "high",
    "cache_dir": "/shared/acrf-ai-cache",
    # "exact_only": True,       # fully deterministic, no tiers
}
```

Or by environment: `ACRF_AI=1`, `ACRF_AI_MODEL`, `ACRF_AI_EFFORT`,
`ACRF_AI_CACHE`.

Needs `anthropic>=0.40`. Without it — or without a credential — the run logs
one warning at startup and continues on the deterministic tiers. It never
fails because AI assist could not start, and never silently produces less
than it claims: the SDK version is checked once, up front, rather than
failing per request mid-run.

## What GenAI is deliberately *not* used for

- Producing annotation text or coordinates. Those come from the mapping specs
  and the layout engine.
- Parsing metadata at run time. That is what profiles are for.
- Deciding SDTM mappings. That is the spec author's job, and it is the part a
  regulator will ask about.
