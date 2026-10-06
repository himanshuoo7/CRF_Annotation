# Validation plan — aCRF Gen (GAMP 5, risk-based)

## Intended use

aCRF Gen produces a **draft** SDTM annotated CRF (aCRF) from a blank CRF and
the EDC study design, for review and approval by a qualified statistical
programmer or data manager. It does not replace that review. The approved
aCRF is part of the sponsor's submission; the sponsor's own process governs
its approval and signature.

## System classification

- **GAMP 5 category:** configurable software with custom components
  (category 4 with category 5 elements). The mapping step uses a
  third-party large language model (Gemini on Vertex AI).
- **Risk control for the AI component:** the model only proposes; every
  proposal is (1) checked against SDTMIG variable naming and re-asked once if
  invalid, (2) restricted to the form's own fields when matching, (3) shown
  to the reviewer with its source (rule, model, fuzzy) in the review
  workbook, and (4) editable, with every edit recorded in the audit trail.
  Deterministic steps (reading inputs, binding pages, placing boxes, writing
  the PDF) are verified by automated tests.

## Requirements and traceability

User requirements, their risk, and the automated tests verifying each live
in [`tools/traceability.py`](../../tools/traceability.py). Every requirement
must name at least one test; every named test must exist and pass.

## Qualification

| Stage | Who | How |
|---|---|---|
| **IQ** — installation | Vajrra, per deployment | Deployed revision and container image recorded; service health and authentication verified; storage lifecycle (7-day deletion) verified |
| **OQ** — operation | Vajrra, per release | `python tools/oq_report.py` runs the full suite and writes `validation/OQ-<version>-<commit>.md`: environment, component versions, each requirement Met / Not met with test evidence. A release is qualified only from a clean, committed tree with all requirements met |
| **PQ** — performance | Customer | The customer runs representative CRFs from their own studies and compares the drafts with their approved aCRFs, using the review workbook and FDA PDF check |

In addition to the unit and integration tests, each release is regression-run
on three real, structurally different studies (a 51-page GSK study, a single
226-field academic oncology form, and an independent ODM test trial with an
author's answer key), and every proposed mapping is checked to reach the PDF.

## Change control

All code, prompts, and documents are version-controlled in git; each change
is a reviewed commit with a description. A change to the mapping prompt or
model is a change to the system and requires a new OQ report. The model in
use is recorded in each job's audit trail (`run_finished`).

## Records

Per job: the aCRF, the review workbook (with each annotation's source and
confidence), the FDA PDF check, and the audit trail. Per release: the OQ
report.
