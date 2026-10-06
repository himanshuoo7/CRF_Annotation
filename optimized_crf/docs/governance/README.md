# Governance

Documents a sponsor's QA, IT security, and regulatory teams ask for before
evaluating aCRF Gen.

| Document | Answers |
|---|---|
| [data-handling.md](data-handling.md) | What data we receive, where it is processed and stored, how long it is kept, who can access it, and what the AI provider may do with it |
| [part11-readiness.md](part11-readiness.md) | 21 CFR Part 11, clause by clause: provided, partial, customer procedure, or gap |
| [validation-plan.md](validation-plan.md) | GAMP 5 classification, AI risk controls, IQ / OQ / PQ, change control |
| [`tools/traceability.py`](../../tools/traceability.py) | User requirements, their risk, and the tests that verify them |
| `validation/OQ-*.md` | The operational qualification report for each release (`python tools/oq_report.py`) |
| Every aCRF's `fda_pdf_check.txt` | The output checked against the FDA PDF Specifications v4.1, rule by rule |

These documents state what is in place today and list what is not. Keep them
true: update them in the same commit as any change they describe.
