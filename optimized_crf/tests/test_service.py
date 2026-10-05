"""The web service: tickets, uploads, a run, downloads and the follow-up chat."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from crf_annotation.config_workbook import read_config_workbook
from service import app as service
from service import chat

SECRET = "test-only-service-secret"
JOB = "job-test-0001"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(service, "SECRET", SECRET)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return TestClient(service.app)


def ticket(job=JOB, exp=None, secret=SECRET):
    return {"X-Acrf-Ticket": service.sign_ticket(
        {"user": "user-1", "job": job, "study": "STUDY123", "exp": exp or time.time() + 600}, secret)}


def upload(client, study, names=("crf", "design")):
    files = []
    if "crf" in names:
        files.append(("files", ("blank_crf.pdf", study["crf"].read_bytes(), "application/pdf")))
    if "design" in names:
        for path in sorted(study["meta"].glob("*")):
            files.append(("files", (path.name, path.read_bytes(), "application/octet-stream")))
    return client.post(f"/v1/jobs/{JOB}/files", files=files, headers=ticket())


def test_every_route_needs_a_valid_ticket_for_that_job(client, study):
    assert client.get(f"/v1/jobs/{JOB}").status_code == 401
    assert client.get(f"/v1/jobs/{JOB}", headers=ticket(job="job-someone-else")).status_code == 401
    assert client.get(f"/v1/jobs/{JOB}", headers=ticket(exp=time.time() - 1)).status_code == 401
    assert client.get(f"/v1/jobs/{JOB}", headers=ticket(secret="forged")).status_code == 401
    assert client.get(f"/v1/jobs/{JOB}", headers=ticket()).status_code == 200
    assert client.get("/v1/jobs/..%2Fetc", headers=ticket(job="../etc")).status_code in (401, 404)


def test_uploads_are_sorted_into_crf_and_design_and_report_what_is_missing(client, study):
    first = upload(client, study, names=("crf",)).json()
    assert first["missing"] == ["design"]
    assert first["state"] == "uploading"
    second = upload(client, study, names=("design",)).json()
    assert second["state"] == "ready"
    assert {f["kind"] for f in second["files"]} == {"crf", "design"}
    assert second["has_specs"] is False


def test_a_run_annotates_from_proposed_specs_and_serves_the_results(client, study):
    upload(client, study)
    response = client.post(f"/v1/jobs/{JOB}/run", headers=ticket())
    assert response.status_code == 200, response.text
    done = response.json()
    assert done["state"] == "done"
    assert all(step["state"] == "done" for step in done["steps"])
    # the live steps come from the pipeline's log lines as it runs
    assert "Proposed" in next(step for step in done["steps"] if step["id"] == "specs").get("note", "")
    assert done["result"]["annotations"] > 0
    assert done["result"]["proposed_specs"]["proposed"] >= 5
    assert "acrf" not in done["result"]  # no server paths leak out

    pdf = client.get(f"/v1/jobs/{JOB}/files/acrf.pdf", headers=ticket())
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert client.get(f"/v1/jobs/{JOB}/files/aCRF_Config.xlsx", headers=ticket()).status_code == 200
    assert client.get(f"/v1/jobs/{JOB}/files/proposed_mappings.json", headers=ticket()).status_code == 200
    assert client.get(f"/v1/jobs/{JOB}/files/status.json", headers=ticket()).status_code == 404
    image = client.get(f"/v1/jobs/{JOB}/pages/1.png", headers=ticket())
    assert image.status_code == 200 and image.content[:4] == b"\x89PNG"
    assert client.get(f"/v1/jobs/{JOB}/pages/99.png", headers=ticket()).status_code == 404


def test_a_run_without_inputs_is_refused(client):
    assert client.post(f"/v1/jobs/{JOB}/run", headers=ticket()).status_code == 400


class FakeModel:
    def __init__(self, answer):
        self.answer = answer

    def json_call(self, **_):
        return self.answer


def test_chat_lists_reviews_without_a_model_and_edits_with_one(client, study):
    upload(client, study)
    client.post(f"/v1/jobs/{JOB}/run", headers=ticket())
    root = service.job_dir(JOB)

    offline = chat.answer(root, "what needs review?", client=None)
    assert offline["edits_applied"] == 0
    assert "review" in offline["reply"].lower()

    coordinates = read_config_workbook(root / "out" / "aCRF_Config.xlsx")
    row = int(coordinates[coordinates["A_TEXT"].astype(str) == "DM.SEX"].index[0])
    edited = chat.answer(root, "change Sex to DM.SEXX", client=FakeModel({
        "reply": "Done — Sex now maps to DM.SEXX.",
        "edits": [{"row": row, "annotation": "DM.SEXX"}, {"row": 99999, "annotation": "DM.X"},
                  {"row": row, "annotation": "drop table; --"}],
    }))
    assert edited["edits_applied"] == 1
    assert edited["result"]["annotations"] > 0
    after = read_config_workbook(root / "out" / "aCRF_Config.xlsx")
    assert after.at[row, "A_TEXT"] == "DM.SEXX"
    assert after.at[row, "MATCH_METHOD"] == "reviewed"
