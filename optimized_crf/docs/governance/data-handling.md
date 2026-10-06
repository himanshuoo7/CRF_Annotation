# Data handling — aCRF Gen

What happens to a customer's files, where, for how long, and who can see them.
Every statement here describes the system as deployed; anything not yet in
place is listed under **Open items**, not claimed.

## What we receive

| Data | Example | Contains patient data? |
|---|---|---|
| Blank CRF (PDF) | the study's case report form, unfilled | No — a blank form |
| EDC study design | CDISC ODM-XML or Medidata Rave ALS export | No — form and field definitions |
| SDTM specs (optional) | mapping specifications (JSON) | No |
| Chat messages | "on page 42, change Outcome to AE.AEOUT" | No |

aCRF Gen works on study *metadata*. It never needs, and is not designed to
receive, subject-level clinical data.

## Where it is processed and stored

| Component | Provider | Location | Holds |
|---|---|---|---|
| Sign-in, accounts, credits, invitations | Cloudflare Workers + D1 | Cloudflare network | name, email, sign-in history, credit ledger |
| Annotation service | Google Cloud Run | asia-south1 (Mumbai) | job files while processing |
| Job storage | Google Cloud Storage | asia-south1 (Mumbai) | inputs, outputs, audit trail |
| Mapping model | Gemini on Google Vertex AI | `global` endpoint (see open items) | the prompt for one request |
| Transactional email | Resend | — | email address, one-time codes |

All traffic is TLS-encrypted in transit. Google Cloud and Cloudflare encrypt
data at rest by default.

## How long we keep it

| Data | Retention |
|---|---|
| Uploaded files, generated aCRF, review workbook, FDA PDF check | 7 days, then deleted automatically (storage lifecycle rule) |
| Per-job audit trail | 7 days, with the job — download it with the aCRF |
| Cached model answers (field label → proposed mapping) | 7 days |
| Account, sign-in history, credit ledger | for the life of the account |
| Service logs (Google Cloud Logging) | Google's default log retention |

## Who can access it

- **Sign-in required** — Google, or email and password with email verification.
- **Invite-only** while the product is in early access; admins grant access per account.
- **Per-job isolation** — a job belongs to one account. Every request to the
  annotation service carries a signed ticket (HMAC-SHA256) naming the user
  and that single job, valid for 15 minutes; any other job is refused.
- **Admins** — a short, named list of Vajrra staff (configured, not self-service).

## The AI model and your data

Mappings are proposed by Gemini, called through **our own Google Cloud
project** on Vertex AI — not a consumer AI service. Google's published terms
for Vertex AI state:

- **No training on customer data.** "Google won't use your data to train or
  fine-tune any AI/ML models without your prior permission or instruction."
  ([Vertex AI zero data retention](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/vertex-ai-zero-data-retention))
- **In-memory caching.** By default Gemini caches inputs and outputs in memory
  only (not at rest), isolated per project, with a 24-hour TTL. It can be
  disabled per project.
- **Abuse monitoring.** Google may log prompts to detect abuse of its
  acceptable-use policy; an exception can be requested.

Vajrra does not train any model on customer files. Model answers are cached
in our own storage (7 days) only so that re-running the same study gives the
same answer at no extra cost.

## Every action is recorded

Each job has an append-only, hash-chained audit trail — who did what, when
(UTC), and the before and after of every annotation change. See
[part11-readiness.md](part11-readiness.md).

## Open items

| Item | Why it matters | Status |
|---|---|---|
| Disable Gemini in-memory caching for the project | strict zero data retention | not yet done (one project setting) |
| Request Google's abuse-monitoring logging exception | strict zero data retention | not yet requested |
| Regional model endpoint instead of `global` | data residency commitments | `global` today |
| Customer-managed encryption keys (CMEK) | enterprise key control | not offered |
| Longer audit-trail retention | Part 11 record retention | 7 days by decision; customers keep the downloaded trail |
| SOC 2 / ISO 27001 | enterprise procurement | not started |
