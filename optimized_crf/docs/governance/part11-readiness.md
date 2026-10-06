# 21 CFR Part 11 readiness — aCRF Gen

21 CFR Part 11 applies to *electronic records and signatures* that a sponsor
relies on. Compliance is reached by the **system and the customer's
procedures together**; no software is "Part 11 compliant" on its own. This
matrix states, clause by clause, what aCRF Gen provides today, what the
customer's own procedures must cover, and what is still missing.

Status: **Provided** · **Partial** · **Customer** (customer SOP) · **Gap**

## Subpart B — Electronic records (§11.10, closed systems; §11.30 open systems)

| Clause | Requirement | Status | How |
|---|---|---|---|
| 11.10(a) | Validation for accuracy, reliability, consistent performance | Partial | Automated test suite and a per-release OQ report ([validation-plan.md](validation-plan.md)); customer performs PQ on their own CRFs |
| 11.10(b) | Accurate and complete copies, human-readable and electronic | Provided | aCRF PDF, review workbook (XLSX), FDA PDF check, audit trail (CSV) all downloadable |
| 11.10(c) | Protection of records for their retention period | Partial | Files and audit trail kept 7 days, then deleted; customer archives the downloaded deliverables in their own system |
| 11.10(d) | Limiting access to authorized individuals | Provided | Sign-in with verified email, per-job signed tickets, admin list |
| 11.10(e) | Secure, computer-generated, time-stamped audit trail of create/modify/delete; previous values not obscured | Provided | Append-only per-job trail: user, UTC timestamp, action, before/after; SHA-256 hash chain reveals any altered or removed entry; exported as CSV |
| 11.10(e) | Audit trail retained as long as the record | Partial | 7 days by decision; customer keeps the exported trail with the aCRF |
| 11.10(f) | Operational checks enforcing permitted sequencing | Provided | Workflow enforced: inputs → run → review/edit; edits refused before a run; edits validated before applied |
| 11.10(g) | Authority checks | Partial | Users act only on their own jobs; no role-based reviewer/approver separation yet |
| 11.10(h) | Device checks for valid input sources | Partial | Upload type classification and size limits; no device binding |
| 11.10(i) | Training of developers, maintainers, users | Customer | Vendor and customer training records |
| 11.10(j) | Written policies holding individuals accountable for e-signatures | Customer | Customer SOP |
| 11.10(k) | Controls over system documentation | Partial | Documentation and code version-controlled in git with change history |
| 11.30 | Open-system controls (encryption etc.) | Provided | TLS in transit, encryption at rest, signed per-job tickets |

## Subpart C — Electronic signatures (§11.50–11.300)

| Clause | Requirement | Status |
|---|---|---|
| 11.50 | Signature manifestation (name, date/time, meaning) on signed records | Gap |
| 11.70 | Signature/record linking | Gap |
| 11.100 | Uniqueness of signatures; identity verification | Partial — unique accounts with verified email; no signature act yet |
| 11.200 | Signature components (e.g. ID + password, re-authentication) | Gap |
| 11.300 | Controls for identification codes/passwords | Partial — managed by the sign-in provider |

**Today aCRF Gen produces a draft for review; it does not capture electronic
signatures.** A sponsor signs the final aCRF in their own Part 11 system.

## Roadmap to close the gaps

1. **Approve and sign** — an "Approve aCRF" step with re-authentication,
   signature meaning, and the signature manifested on the PDF and in the
   audit trail (11.50, 11.70, 11.200).
2. **Roles** — author / reviewer / approver separation (11.10(g)).
3. **Retention option** — keep audit trails beyond 7 days for customers who
   need it (11.10(c), 11.10(e)).
4. **Validation package** — IQ/OQ documents per release, PQ template for the
   customer (11.10(a)).

## How to describe it accurately

- ✅ "Part 11-ready audit trail: every change recorded with user, time, and
  before/after values, tamper-evident."
- ✅ "Supports your Part 11 compliance as part of your validated process."
- ❌ "Part 11 compliant" (requires e-signatures, validation, and customer SOPs).
