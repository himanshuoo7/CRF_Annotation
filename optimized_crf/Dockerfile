# The aCRF web service (service/app.py) for Cloud Run.
#   gcloud run deploy acrf --source . --region asia-south1 ...  (see service/DEPLOY.md)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 ACRF_DATA_DIR=/data
WORKDIR /app

COPY requirements.txt ./requirements.txt
COPY service/requirements.txt ./service-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r service-requirements.txt

COPY pyproject.toml README.md ./
COPY crf_annotation ./crf_annotation
COPY service ./service
RUN pip install --no-cache-dir --no-deps . && useradd --create-home acrf && mkdir -p /data && chown acrf /data
USER acrf

CMD exec uvicorn service.app:app --host 0.0.0.0 --port "$PORT" --workers 1 --timeout-keep-alive 75
