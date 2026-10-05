"""aCRF web service: the annotation pipeline behind vajrra.ai's "try it" chat.

Runs on Cloud Run. It never sees a browser directly: every request comes from
the Vajrra accounts service (accounts.vajrra.ai), which signs people in,
enforces the one-free-annotation rule and credits, and forwards the request
with a short-lived ticket (`X-Acrf-Ticket`) naming the user and the job. A
ticket is an HMAC over its payload with ACRF_SERVICE_SECRET, shared by the
two services; a job can only ever be touched with a ticket for that job.

Jobs live under ACRF_DATA_DIR (a Cloud Storage bucket mounted as a volume in
production, a local folder in development):

    jobs/<job id>/inputs/{crf,design,specs}/   what was uploaded
    jobs/<job id>/out/                         acrf.pdf, aCRF_Config.xlsx, proposed_specs/
    jobs/<job id>/status.json                  state, live steps, result

Files older than ACRF_RETENTION_DAYS are deleted (the bucket also has a
lifecycle rule; the sweep here is the belt to its braces).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from crf_annotation import MatchingOptions, annotate_crf
from crf_annotation.job import CrfAnnotationError

DATA_DIR = Path(os.getenv("ACRF_DATA_DIR", "./data")).resolve()
SECRET = os.getenv("ACRF_SERVICE_SECRET", "")
RETENTION_DAYS = float(os.getenv("ACRF_RETENTION_DAYS", "7"))
MAX_UPLOAD_BYTES = int(os.getenv("ACRF_MAX_UPLOAD_BYTES", str(80 * 1024 * 1024)))
JOB_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")
OUTPUT_FILES = {"acrf.pdf", "aCRF_Config.xlsx", "proposed_mappings.json"}

app = FastAPI(title="aCRF service", docs_url=None, redoc_url=None, openapi_url=None)
logger = logging.getLogger("acrf.service")


# --- tickets ---------------------------------------------------------------
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_ticket(payload: dict[str, Any], secret: Optional[str] = None) -> str:
    """`<base64url JSON>.<base64url HMAC-SHA256>` — the accounts service makes these."""
    secret = SECRET if secret is None else secret
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())}"


def verify_ticket(ticket: Optional[str], job_id: str, secret: Optional[str] = None) -> dict[str, Any]:
    secret = SECRET if secret is None else secret
    if not secret or not ticket or ticket.count(".") != 1:
        raise HTTPException(401, "unauthorized")
    body, signature = ticket.split(".")
    expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "unauthorized")
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError as exc:
        raise HTTPException(401, "unauthorized") from exc
    if payload.get("job") != job_id or float(payload.get("exp", 0)) < time.time():
        raise HTTPException(401, "unauthorized")
    return payload


# --- jobs ------------------------------------------------------------------
def job_dir(job_id: str) -> Path:
    if not JOB_ID.match(job_id):
        raise HTTPException(404, "not_found")
    return DATA_DIR / "jobs" / job_id


def read_status(job_id: str) -> dict[str, Any]:
    try:
        return json.loads((job_dir(job_id) / "status.json").read_text())
    except (OSError, ValueError):
        return {"state": "empty", "files": [], "steps": []}


def write_status(job_id: str, status: dict[str, Any]) -> None:
    path = job_dir(job_id) / "status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status))
    temporary.replace(path)


def classify(name: str, content: bytes) -> str:
    """What an uploaded file is: crf, design, specs, or unknown."""
    lower = name.lower()
    if lower.endswith(".pdf"):
        return "crf"
    if lower.endswith((".xml", ".odm", ".csv", ".xls", ".xlsx")):
        return "design"
    if lower.endswith(".json"):
        try:
            data = json.loads(content)
        except ValueError:
            return "unknown"
        if isinstance(data, dict) and "domain" in data and ("variables" in data or "value_level" in data):
            return "specs"
        return "design"
    return "unknown"


def summarise_inputs(job_id: str) -> dict[str, Any]:
    root = job_dir(job_id) / "inputs"
    files = [{"name": p.name, "kind": p.parent.name} for kind in ("crf", "design", "specs")
             for p in sorted((root / kind).glob("*")) if p.is_file()]
    kinds = {f["kind"] for f in files}
    missing = [k for k in ("crf", "design") if k not in kinds]
    return {"files": files, "missing": missing, "has_specs": "specs" in kinds}


STEPS = (
    ("crf", "Reading your blank CRF"),
    ("design", "Reading the EDC study design"),
    ("specs", "Preparing SDTM mappings"),
    ("match", "Matching CRF text to EDC fields"),
    ("annotate", "Placing annotations and bookmarks"),
)

# log line → the step it finishes
_STEP_MARKERS = (
    ("Job: mode", "crf"),
    ("Study design from", "design"),
    ("Spec catalog", "specs"),
    ("Form/field catalogue", "match"),
    ("Run complete", "annotate"),
)


class _StepHandler(logging.Handler):
    """Turns the pipeline's own log lines into the chat's live steps."""

    def __init__(self, job_id: str) -> None:
        super().__init__(logging.INFO)
        self.job_id = job_id

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        for marker, step in _STEP_MARKERS:
            if marker in message:
                status = read_status(self.job_id)
                for item in status.get("steps", []):
                    if item["id"] == step:
                        item["state"] = "done"
                        if step == "specs" and "Proposed" in message:
                            item["note"] = message
                status["steps"] = _advance(status.get("steps", []))
                write_status(self.job_id, status)


def _advance(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first step not done is the running one."""
    running = False
    for item in steps:
        if item["state"] == "done":
            continue
        item["state"] = "pending" if running else "running"
        running = True
    return steps


def sweep_old_jobs() -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    root = DATA_DIR / "jobs"
    for path in root.glob("*") if root.exists() else ():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


# --- routes ----------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "service": "acrf"}


@app.post("/v1/jobs/{job_id}/files")
async def upload(job_id: str, files: list[UploadFile] = File(...),
                 x_acrf_ticket: Optional[str] = Header(default=None)) -> dict[str, Any]:
    verify_ticket(x_acrf_ticket, job_id)
    if read_status(job_id).get("state") == "running":
        raise HTTPException(409, "running")
    total = 0
    for upload_file in files:
        content = await upload_file.read()
        total += len(content)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "too_large")
        name = SAFE_NAME.sub("_", Path(upload_file.filename or "upload").name)[:120] or "upload"
        kind = classify(name, content)
        if kind == "unknown":
            continue
        target = job_dir(job_id) / "inputs" / kind
        if kind == "crf":
            shutil.rmtree(target, ignore_errors=True)  # one blank CRF per job
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_bytes(content)
    summary = summarise_inputs(job_id)
    status = read_status(job_id)
    status.update({"state": "ready" if not summary["missing"] else "uploading", **summary})
    write_status(job_id, status)
    return status


@app.get("/v1/jobs/{job_id}")
def status(job_id: str, x_acrf_ticket: Optional[str] = Header(default=None)) -> dict[str, Any]:
    verify_ticket(x_acrf_ticket, job_id)
    return read_status(job_id)


_RUN_LOCK = threading.Lock()


@app.post("/v1/jobs/{job_id}/run")
def run(job_id: str, x_acrf_ticket: Optional[str] = Header(default=None)) -> JSONResponse:
    ticket = verify_ticket(x_acrf_ticket, job_id)
    summary = summarise_inputs(job_id)
    if summary["missing"]:
        raise HTTPException(400, "missing_inputs")
    root = job_dir(job_id)
    status_now = read_status(job_id)
    if status_now.get("state") == "running":
        raise HTTPException(409, "running")
    steps = [{"id": step, "label": label, "state": "pending"} for step, label in STEPS]
    if not summary["has_specs"]:
        steps[2]["label"] = "Proposing SDTM mappings with CDISC rules and AI"
    write_status(job_id, {**summary, "state": "running", "steps": _advance(steps), "started": time.time()})

    handler = _StepHandler(job_id)
    pipeline_logger = logging.getLogger("crf_annotation")
    pipeline_logger.addHandler(handler)
    pipeline_logger.setLevel(logging.INFO)
    try:
        result = annotate_crf(
            crf=root / "inputs" / "crf",
            output_dir=root / "out",
            specs=(root / "inputs" / "specs") if summary["has_specs"] else None,
            design=root / "inputs" / "design",
            study=str(ticket.get("study") or "Study"),
            author="Vajrra aCRF",
            matching=MatchingOptions(propose_specs=not summary["has_specs"],
                                     cache_dir=str(DATA_DIR / "ai_cache")),
        )
    except CrfAnnotationError as exc:
        write_status(job_id, {**read_status(job_id), "state": "failed", "error": str(exc)})
        return JSONResponse(read_status(job_id), status_code=422)
    except Exception:  # noqa: BLE001 - one failed job must not take the service down
        logger.exception("job %s failed", job_id)
        write_status(job_id, {**read_status(job_id), "state": "failed",
                              "error": "Something went wrong while annotating. Our team has been notified."})
        return JSONResponse(read_status(job_id), status_code=500)
    finally:
        pipeline_logger.removeHandler(handler)

    done = read_status(job_id)
    for item in done.get("steps", []):
        item["state"] = "done"
    data = result.to_dict()
    data.pop("acrf", None)
    data.pop("config", None)
    done.update({"state": "done", "result": data, "finished": time.time()})
    write_status(job_id, done)
    sweep_old_jobs()
    return JSONResponse(done)


@app.get("/v1/jobs/{job_id}/files/{name}")
def download(job_id: str, name: str, x_acrf_ticket: Optional[str] = Header(default=None)) -> FileResponse:
    verify_ticket(x_acrf_ticket, job_id)
    if name not in OUTPUT_FILES:
        raise HTTPException(404, "not_found")
    out = job_dir(job_id) / "out"
    path = out / "proposed_specs" / name if name == "proposed_mappings.json" else out / name
    if not path.is_file():
        raise HTTPException(404, "not_found")
    return FileResponse(path, filename=name)


@app.get("/v1/jobs/{job_id}/pages/{page}.png")
def page_image(job_id: str, page: int, x_acrf_ticket: Optional[str] = Header(default=None)) -> Response:
    verify_ticket(x_acrf_ticket, job_id)
    import pymupdf

    path = job_dir(job_id) / "out" / "acrf.pdf"
    if not path.is_file():
        raise HTTPException(404, "not_found")
    with pymupdf.open(path) as document:
        if not 1 <= page <= document.page_count:
            raise HTTPException(404, "not_found")
        png = document[page - 1].get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4)).tobytes("png")
    return Response(png, media_type="image/png", headers={"cache-control": "private, max-age=300"})


class ChatMessage(BaseModel):
    message: str


@app.post("/v1/jobs/{job_id}/chat")
def chat(job_id: str, body: ChatMessage, x_acrf_ticket: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Questions about the result, and plain-English edits that re-render it."""
    ticket = verify_ticket(x_acrf_ticket, job_id)
    from .chat import answer

    return answer(job_dir(job_id), body.message[:2000], study=str(ticket.get("study") or "Study"))
