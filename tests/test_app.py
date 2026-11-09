import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import io
import json

from fastapi.testclient import TestClient
import pytest

from app import create_app, inspect_csv, preview_intake

KEY = b"test-key-only"
TASK = {"title": "Send the agreed proposal", "owner": "Mira", "due_date": "2026-11-13", "priority": "high"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "test.sqlite3", signing_key=KEY)) as test_client:
        yield test_client


def signed_event(event_id="evt_test", task=None, indent=None):
    body = json.dumps({"id": event_id, "type": "task.requested", "task": task or TASK}, indent=indent).encode()
    signature = "sha256=" + hmac.new(KEY, body, hashlib.sha256).hexdigest()
    return body, {"Content-Type": "application/json", "X-Lab-Signature": signature}


def test_intake_extracts_explicit_fields():
    result = preview_intake("Prepare the invoice by 2026-11-13. Assign to Mira. Priority: high.")
    assert result == {"title": "Prepare the invoice", "owner": "Mira", "due_date": "2026-11-13", "priority": "high", "missing": []}


@pytest.mark.parametrize("message", ["Review the invoice.", "Review the invoice by 2026-99-30."])
def test_intake_does_not_invent_missing_fields(message):
    result = preview_intake(message)
    assert result["owner"] == result["due_date"] == ""
    assert set(result["missing"]) == {"owner", "due_date"}


def test_task_write_status_and_counts(client):
    before = client.get("/api/state").json()
    response = client.post("/api/tasks", json=TASK)
    assert response.status_code == 201
    task = response.json()
    assert client.patch("/api/tasks/" + str(task["id"]), json={"status": "done"}).status_code == 200
    after = client.get("/api/state").json()
    assert after["metrics"]["total"] == before["metrics"]["total"] + 1
    assert after["metrics"]["done"] == before["metrics"]["done"] + 1
    assert next(row for row in after["tasks"] if row["id"] == task["id"])["status"] == "done"


@pytest.mark.parametrize("changes", [{"owner": " "}, {"due_date": "not-a-date"}, {"priority": "urgent"}, {"title": "x" * 161}, {"owner": "Mira\nRowan"}, {"extra": "unexpected"}])
def test_task_validation_does_not_write(client, changes):
    before = client.get("/api/state").json()["metrics"]["total"]
    assert client.post("/api/tasks", json={**TASK, **changes}).status_code == 422
    assert client.get("/api/state").json()["metrics"]["total"] == before


def test_missing_task_returns_404(client):
    assert client.patch("/api/tasks/999999", json={"status": "done"}).status_code == 404


def test_csv_normalisation_quoted_fields_and_duplicates():
    result = inspect_csv('\ufeffName,Email,Company\n"Mira, Lane", MIRA@example.com ,Cedar\nDuplicate,mira@example.com,Cedar\nNoor,noor@example.com,Northline')
    assert (result["total"], result["ready"], result["review"]) == (3, 2, 1)
    assert result["rows"][0]["name"] == "Mira, Lane"
    assert result["rows"][1]["issues"] == ["Duplicate email"]
    assert list(csv.DictReader(io.StringIO(result["csv"])))[0]["email"] == "mira@example.com"


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_csv_export_neutralises_spreadsheet_formulas(prefix):
    result = inspect_csv("name,email,company\nMira,mira@example.com," + prefix + "1+1")
    assert result["escaped"] == 1
    assert list(csv.DictReader(io.StringIO(result["csv"])))[0]["company"] == "'" + prefix + "1+1"


@pytest.mark.parametrize("text", ["name,company\nMira,Cedar", "name,email,email\nMira,x@example.com,x@example.com", "name,email\nMira,\x00x@example.com", "name,email\nMira,x@example.com\n" * 0])
def test_csv_bad_headers_and_control_characters_rejected(text):
    with pytest.raises(ValueError):
        inspect_csv(text)


def test_csv_column_mismatch_and_invalid_rows_not_exported():
    result = inspect_csv("name,email\nMira,mira@example.com,extra\n,missing@example.com\nRowan,invalid\nNoor,noor@example.com")
    assert (result["ready"], result["review"]) == (1, 3)
    assert len(list(csv.DictReader(io.StringIO(result["csv"])))) == 1


def test_csv_row_limit():
    text = "name,email\n" + "\n".join("Person,user" + str(i) + "@example.com" for i in range(501))
    with pytest.raises(ValueError, match="500"):
        inspect_csv(text)


def test_signed_event_and_repeat_create_only_one_task(client):
    before = client.get("/api/state").json()["metrics"]["total"]
    body, headers = signed_event()
    first = client.post("/api/webhooks", content=body, headers=headers)
    second = client.post("/api/webhooks", content=body, headers=headers)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["task_id"] == second.json()["task_id"]
    result = client.get("/api/state").json()
    assert result["metrics"]["total"] == before + 1
    assert result["metrics"]["accepted"] == result["metrics"]["duplicate"] == 1


def test_idempotency_ignores_json_whitespace_but_rejects_changed_payload(client):
    body, headers = signed_event()
    assert client.post("/api/webhooks", content=body, headers=headers).status_code == 201
    pretty_body, pretty_headers = signed_event(indent=2)
    assert client.post("/api/webhooks", content=pretty_body, headers=pretty_headers).status_code == 200
    changed_body, changed_headers = signed_event(task={**TASK, "title": "A different task"})
    assert client.post("/api/webhooks", content=changed_body, headers=changed_headers).status_code == 409


@pytest.mark.parametrize("signature", ["sha256=" + "0" * 64, "not-a-signature", "非ASCII", ""])
def test_invalid_signatures_cannot_create_tasks(client, signature):
    before = client.get("/api/state").json()["metrics"]["total"]
    body, headers = signed_event()
    headers["X-Lab-Signature"] = signature.encode()
    response = client.post("/api/webhooks", content=body, headers=headers)
    assert response.status_code == 401
    assert client.get("/api/state").json()["metrics"]["total"] == before


def test_concurrent_replays_are_atomic(client):
    before = client.get("/api/state").json()["metrics"]["total"]
    body, headers = signed_event("evt_concurrent")
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post("/api/webhooks", content=body, headers=headers), range(4)))
    assert sorted(response.status_code for response in responses) == [200, 200, 200, 201]
    assert client.get("/api/state").json()["metrics"]["total"] == before + 1


def test_simulator_and_reset_are_real_database_operations(client):
    before = client.get("/api/state").json()["metrics"]["total"]
    assert client.post("/api/events/simulate", json={"event_id": "evt_sim"}).json()["outcome"] == "accepted"
    assert client.post("/api/events/simulate", json={"event_id": "evt_sim"}).json()["outcome"] == "duplicate"
    assert client.post("/api/events/simulate", json={"event_id": "evt_bad", "signature": "invalid"}).json()["outcome"] == "rejected"
    assert client.get("/api/state").json()["metrics"]["total"] == before + 1
    assert client.post("/api/reset", json={}).status_code == 200
    after = client.get("/api/state").json()
    assert after["metrics"]["total"] == before
    assert after["attempts"] == []


def test_payload_boundary(client):
    assert client.post("/api/intake/preview", json={"message": "x" * 2001}).status_code == 422
    assert client.post("/api/tasks", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/api/tasks", content="{}", headers={"Content-Type": "application/json", "Content-Length": "200001"}).status_code == 413


def test_malformed_csv_header_is_a_validation_error(client):
    response = client.post("/api/import/preview", json={"csv_text": '"name,email\nMira,mira@example.com'})
    assert response.status_code == 422
    assert "quoting" in response.json()["detail"]


def test_invalid_signed_payload_does_not_create_task(client):
    before = client.get("/api/state").json()["metrics"]["total"]
    body, headers = signed_event(task={**TASK, "owner": ""})
    assert client.post("/api/webhooks", content=body, headers=headers).status_code == 422
    assert client.get("/api/state").json()["metrics"]["total"] == before


def test_database_survives_app_restart(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    with TestClient(create_app(path, signing_key=KEY)) as first:
        task_id = first.post("/api/tasks", json=TASK).json()["id"]
    with TestClient(create_app(path, signing_key=KEY)) as second:
        assert any(task["id"] == task_id and task["title"] == TASK["title"] for task in second.get("/api/state").json()["tasks"])


def test_static_page_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Workflow Lab" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
