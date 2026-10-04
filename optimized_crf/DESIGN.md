# Design notes

Why the pipeline is built the way it is. These are the decisions that are
expensive to reverse later, recorded so they are argued with deliberately
rather than drifted away from.

## The config workbook is the source of truth

The PDF is an output, never an input. Every annotation that gets drawn exists
first as a row in `aCRF_Config.xlsx`, and the only supported way to correct a
run is to edit that workbook and run again.

This buys three things. A reviewer can see and change every decision in a
format they already use. A rerun is a deterministic replay, so a signed-off
study produces identical output forever. And there is one artefact to archive
for an audit rather than a PDF whose provenance is lost.

The consequence to accept: hand-editing the PDF is unsupported and will be
overwritten. That is the right trade for a regulated deliverable.

## One coordinate convention

PDF user space, origin bottom-left, serialised `[x0, y0, x1, y1]` — which is
what the workbook's `LEFT_BOTTOM_RIGHT_TOP` column means. Exactly one module
(`annotate.py`) converts to the top-left origin that PDF libraries use.

Page dimensions are read from each page, never assumed. A4 and landscape CRFs
are common enough that a hard-coded US Letter size is a latent bug, not a
simplification.

## EDC independence through a canonical model

Every EDC describes the same four things — forms, the fields on them, visits,
and which forms are collected at which visit. They disagree only on file
format and vocabulary. Adapters translate a vendor export into one
`StudyDesign`; nothing downstream knows which system a study was built in.

Supporting CDISC ODM-XML is what makes this tractable in practice: it is the
interchange standard, so one adapter covers Veeva Vault CDMS, Medrio,
OpenClinica, Castor and Viedoc. Anything else can be described by a
declarative profile with no code, which keeps new-format support a data
change that can be reviewed and version-controlled.

Which form a page belongs to is a *separate* vendor-specific problem — some
CRFs print a banner, some put the name in a header band. It is a strategy, and
the one that binds the most pages wins.

## Matching is tiered, and every tier is recorded

1. **exact** — normalised string equality
2. **fuzzy** — deterministic, standard library only
3. **model** — only the residue, only from that form's field list

Each tier sees only what the previous could not resolve, so cost scales with
difficulty rather than study size. A clean study never invokes tier 3 at all.

Every match records its method, confidence and reason in the workbook. A
reviewer filters on "not exact" and sees the entire guessed surface of a run
on one screen. This is the mechanism that makes tier 3 acceptable at all.

## Where a model is allowed, and where it is not

**The model produces artefacts a human approves. It never produces the
deliverable.**

It may propose a match for text the deterministic tiers cannot resolve, and it
may propose a parser profile for an unfamiliar export. Both land in front of a
person before they take effect, both are cached by content hash so a rerun
makes no calls, and both can be switched off entirely.

It may not produce annotation text, choose coordinates, or decide an SDTM
mapping. Those come from the mapping specs, which are the controlled document.

`--exact-only` gives a fully deterministic run with no fuzzy tier and no model,
for validation runs and for reproducing a historical output.

## Annotations are keyed on dataset *and* variable

A variable name alone is not unique — the same field name can appear on two
forms and mean different things. Matching on the name alone silently annotates
both with each other's domains.

The default (`auto`) uses the dataset-qualified join when the spec's source
datasets line up with the EDC form OIDs, and falls back to variable-only with
a prominent warning when they do not, because some builds genuinely name them
differently and a strict join would annotate nothing at all. `strict` and
`off` force either behaviour.

## Only collected variables are annotated

An aCRF labels what the *form captures*. Variables that are derived or
assigned during dataset creation — `USUBJID`, `--SEQ`, `EPOCH`, `--DY` — are
not collected anywhere on the page, so CDISC conventions document them in
define.xml with an origin of Derived or Assigned rather than annotating them.
Annotating one is a routine QC finding.

The catalogue drops them by default and logs what it dropped, so a spec that
maps a derived variable does not silently produce a wrong annotation.
`include_derived` overrides it for the rare build that genuinely needs one.

## Every collected field gets an annotation

A reviewer reading the aCRF should never have to wonder whether a blank field
means "not submitted" or "we forgot". So a field the EDC collects and the
specs do not map is annotated `NOT SUBMITTED` beside the field, in grey with a
dotted border so it reads as the absence of a mapping rather than as another
domain.

That is what the FDA guide asks for, and it is also the honest behaviour: the
tool already knew about those fields, and leaving them blank hid a known fact
rather than reporting it. The `unmap` sheet still lists them, so a spec gap is
still visible as a spec gap.

## Bookmarks preserve the reader's zoom

Destinations are `/XYZ` with null left, top and zoom — "Inherit Zoom". The FDA
Study Data Technical Conformance Guide asks for this. The alternative, `/Fit`,
re-zooms to the whole page on every jump, which on a dense CRF makes the
annotations too small to read.

## Failures fail

A run that cannot produce a correct aCRF raises. It does not return a
successful-looking result with an empty or partial PDF.

This matters more here than in most software: an aCRF is a submission
deliverable, and a silently empty one is far worse than a loud failure. The
only tolerant paths are genuinely optional — a missing visit matrix costs
bookmarks, not annotations, and one unplaceable annotation is counted and
logged rather than aborting the run.

## Operational guarantees

- The scratch workspace is context-managed and removed on every exit path.
- Publishing is atomic — artefacts are staged and renamed, so a crash cannot
  leave a truncated PDF that still looks like a deliverable.
- An advisory lock refuses two concurrent runs into one output directory,
  rather than letting them interleave their published files.
- Logs rotate. Per-row detail is `DEBUG`; a 300-page study writes ~30 lines at
  `INFO`.
- The result object carries counts worth alerting on, `needs_review` chief
  among them.

## What is deliberately out of scope

- **OCR.** A scanned CRF with no text layer cannot be annotated.
- **Authoring mappings.** The specs are an input, written by someone who owns
  that decision.
- **Replacing review.** The tool makes review faster and evidenced. It does
  not remove it.
