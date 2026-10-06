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


# --- audit trail ------------------------------------------------------------
def test_the_audit_chain_detects_tampering(tmp_path):
    from service import audit

    actor = {"user_id": "u1", "email": "reviewer@example.com", "name": "Reviewer"}
    audit.record(tmp_path, actor, "file_uploaded", file="crf.pdf", sha256="ab")
    audit.record(tmp_path, actor, "annotation_changed", page=1, before="DM.SEX", after="DM.SEXX")
    assert audit.verify(tmp_path) == (True, None)
    assert [e["seq"] for e in audit.entries(tmp_path)] == [1, 2]
    assert b"reviewer@example.com" in audit.to_csv(tmp_path)

    path = tmp_path / audit.AUDIT_FILE
    path.write_text(path.read_text().replace("DM.SEXX", "DM.RACE"))   # rewrite history
    assert audit.verify(tmp_path) == (False, 2)
    assert b"BROKEN at entry 2" in audit.to_csv(tmp_path)


def test_every_user_action_lands_in_the_audit_trail(client, study):
    upload(client, study)
    client.post(f"/v1/jobs/{JOB}/run", headers=ticket())
    client.get(f"/v1/jobs/{JOB}/files/acrf.pdf", headers=ticket())

    from service import audit

    trail = audit.entries(service.job_dir(JOB))
    actions = [entry["action"] for entry in trail]
    assert actions.count("file_uploaded") >= 2
    assert "run_started" in actions and "run_finished" in actions and "file_downloaded" in actions
    assert all(entry["actor"]["user_id"] == "user-1" for entry in trail)
    finished = next(entry for entry in trail if entry["action"] == "run_finished")
    assert len(finished["outputs"]["acrf.pdf"]) == 64                 # the delivered PDF's SHA-256
    csv_text = client.get(f"/v1/jobs/{JOB}/files/audit_trail.csv", headers=ticket()).text
    assert "Hash chain: intact" in csv_text and "run_finished" in csv_text


# --- chat edits: remove, add, move -------------------------------------------
def _line(root, text):
    from service.chat import _Page

    items = _Page(root).items
    return int(items.index[items["ITM_TEXT"].astype(str) == text][0])


def test_chat_can_remove_add_and_move_annotations_and_each_is_audited(client, study, monkeypatch):
    upload(client, study)
    client.post(f"/v1/jobs/{JOB}/run", headers=ticket())
    root = service.job_dir(JOB)
    fields = read_config_workbook(root / "out" / "aCRF_Config.xlsx")
    race = int(fields[fields["A_TEXT"].astype(str) == "DM.RACE"].index[0])
    sex = int(fields[fields["A_TEXT"].astype(str) == "DM.SEX"].index[0])

    removed = chat.answer(root, "remove the race box on page 1", client=FakeModel({
        "reply": "Removed.", "edits": [{"action": "remove", "row": race, "line": -1, "annotation": ""}]}))
    assert removed["changes"][0]["action"] == "annotation_removed"
    after = read_config_workbook(root / "out" / "aCRF_Config.xlsx")
    assert "DM.RACE" not in set(after["A_TEXT"].astype(str))

    added = chat.answer(root, "add DM.RACE next to Race on page 1", client=FakeModel({
        "reply": "Added.", "edits": [{"action": "add", "row": -1, "line": _line(root, "Race"), "annotation": "DM.RACE"}]}))
    change = added["changes"][0]
    assert change["action"] == "annotation_added" and change["page"] == 1 and change["after"] == "DM.RACE"
    after = read_config_workbook(root / "out" / "aCRF_Config.xlsx")
    new = after[after["A_TEXT"].astype(str) == "DM.RACE"].iloc[0]
    assert new["ITM_TEXT"] == "Race" and new["MATCH_METHOD"] == "reviewed" and new["POS1"] != "[0, 0, 0, 0]"

    sex = int(after[after["A_TEXT"].astype(str) == "DM.SEX"].index[0])
    moved = chat.answer(root, "move the sex box beside Date of Birth on page 1", client=FakeModel({
        "reply": "Moved.", "edits": [{"action": "move", "row": sex, "line": _line(root, "Date of Birth"),
                                      "annotation": ""}]}))
    assert moved["changes"][0]["action"] == "annotation_moved"
    assert "Date of Birth" in moved["changes"][0]["after"]

    # the service route writes each of these to the job's audit trail
    monkeypatch.setattr(chat, "_client", lambda: FakeModel({
        "reply": "Changed.", "edits": [{"action": "change", "row": sex, "line": -1, "annotation": "DM.SEXX"}]}))
    client.post(f"/v1/jobs/{JOB}/chat", json={"message": "make sex DM.SEXX"}, headers=ticket())
    from service import audit

    trail = audit.entries(root)
    edit = next(entry for entry in trail if entry["action"] == "annotation_changed")
    assert (edit["before"], edit["after"], edit["via"]) == ("DM.SEX", "DM.SEXX", "chat")
    assert [e["action"] for e in trail][-3:] == ["annotation_changed", "rerendered", "chat_reply"]
    assert audit.verify(root) == (True, None)


def test_an_add_that_would_duplicate_is_refused_and_said_so(client, study):
    upload(client, study)
    client.post(f"/v1/jobs/{JOB}/run", headers=ticket())
    root = service.job_dir(JOB)
    reply = chat.answer(root, "add DM.SEX next to Sex on page 1", client=FakeModel({
        "reply": "Added.", "edits": [{"action": "add", "row": -1, "line": _line(root, "Sex"), "annotation": "DM.SEX"}]}))
    assert reply["edits_applied"] == 0
    assert reply["changes"][0]["action"] == "edit_refused" and "already has DM.SEX" in reply["changes"][0]["reason"]
    assert "Not applied" in reply["reply"]


def test_the_fda_pdf_check_is_served_with_the_acrf(client, study):
    upload(client, study)
    done = client.post(f"/v1/jobs/{JOB}/run", headers=ticket()).json()
    assert set(done["result"]["fda_pdf_check"]) == {"pass", "fail", "warn", "info"}
    report = client.get(f"/v1/jobs/{JOB}/files/fda_pdf_check.txt", headers=ticket())
    assert report.status_code == 200 and "FDA PDF Specifications" in report.text
