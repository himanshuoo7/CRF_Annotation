# Deploying the aCRF service to Cloud Run

The service sits behind accounts.vajrra.ai/acrf, which signs every request with
a short-lived ticket (`X-Acrf-Ticket`, HMAC with `ACRF_SERVICE_SECRET`). It is
deployed with `--allow-unauthenticated` because the ticket is the auth.

One-time setup (project `divyastra-510704`, billing enabled, Claude enabled in
Vertex AI Model Garden):

```sh
PROJECT=divyastra-510704 REGION=asia-south1 BUCKET=divyastra-acrf-jobs
gcloud config set project $PROJECT
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com aiplatform.googleapis.com secretmanager.googleapis.com
# job files, deleted after 7 days
gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":7}}]}' > /tmp/lifecycle.json
gcloud storage buckets update gs://$BUCKET --lifecycle-file=/tmp/lifecycle.json
# the shared ticket secret (never printed; the same value goes to the Worker)
openssl rand -base64 48 | tr -d '\n' | gcloud secrets create acrf-service-secret --data-file=-
```

The Cloud Run service account needs `roles/aiplatform.user` (Claude on Vertex),
`roles/storage.objectAdmin` on the bucket and `roles/secretmanager.secretAccessor`.

Deploy (from `optimized_crf/`):

```sh
gcloud run deploy acrf --source . --region $REGION --allow-unauthenticated \
  --memory 2Gi --cpu 2 --timeout 900 --concurrency 4 --max-instances 3 \
  --execution-environment gen2 \
  --add-volume name=jobs,type=cloud-storage,bucket=$BUCKET \
  --add-volume-mount volume=jobs,mount-path=/data \
  --set-secrets ACRF_SERVICE_SECRET=acrf-service-secret:latest \
  --set-env-vars ACRF_VERTEX_PROJECT=$PROJECT,ACRF_VERTEX_REGION=global,ACRF_AI_CACHE=/data/.ai-cache
```

Then point the Worker at it: set `ACRF_SERVICE_URL` in
`cloudflare/control-plane/wrangler.jsonc` to the service URL, pipe the same
secret into `wrangler secret put ACRF_SERVICE_SECRET`, and `wrangler deploy`.
