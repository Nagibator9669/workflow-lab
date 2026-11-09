"""Three small automation demonstrations with real, local persistence."""

from contextlib import contextmanager
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

BASE = Path(__file__).resolve().parent
MAX_BODY = 200_000
Priority = Literal["low", "medium", "high"]
Status = Literal["new", "in_progress", "done"]


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=160)
    owner: str = Field(min_length=1, max_length=80)
    due_date: date
    priority: Priority = "medium"

    @field_validator("title", "owner")
    @classmethod
    def single_line(cls, value):
        if any(ord(character) < 32 for character in value):
            raise ValueError("Use a single line without control characters.")
        return value


class IntakeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=2000)


class ImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csv_text: str = Field(min_length=1, max_length=120_000)


class StatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Status


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", max_length=80)
    type: Literal["task.requested"]
    task: TaskInput


class SimulationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", max_length=80)
    signature: Literal["valid", "invalid"] = "valid"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def preview_intake(message):
    """A deliberately small extractor, not an LLM or a natural-language guarantee."""
    first_line = re.split(r"[.!?\n]", message, maxsplit=1)[0].strip()
    title = re.sub(r"\s+by\s+\d{4}-\d{2}-\d{2}\b.*$", "", first_line, flags=re.I).strip()
    owner_match = re.search(r"\bassign(?:ed)?\s+to\s+([^.\n,;]+)", message, re.I)
    owner = owner_match.group(1).strip() if owner_match else ""
    date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", message)
    due = ""
    if date_match:
        try:
            due = date.fromisoformat(date_match.group(1)).isoformat()
        except ValueError:
            pass
    priority_match = re.search(r"\bpriority\s*:?\s*(low|medium|high)\b", message, re.I)
    priority = priority_match.group(1).lower() if priority_match else "medium"
    missing = [key for key, value in {"title": title, "owner": owner, "due_date": due}.items() if not value]
    return {"title": title, "owner": owner, "due_date": due, "priority": priority, "missing": missing}


# shortcut: this is a practical email-format check; verify addresses before real email delivery.
EMAIL = re.compile(r"^[^@\s,;<>]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")


def spreadsheet_safe(value):
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def inspect_csv(text):
    if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        raise ValueError("Remove control characters from the CSV file.")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), strict=True)
    try:
        headers = [item.strip().lower() for item in (reader.fieldnames or [])]
    except csv.Error as error:
        raise ValueError("Check CSV header quoting: " + str(error)) from error
    if not {"name", "email"}.issubset(headers):
        raise ValueError("Include name and email columns. A company column is optional.")
    if len(headers) != len(set(headers)) or len(headers) > 30:
        raise ValueError("Use unique column names, with no more than 30 columns.")
    reader.fieldnames = headers
    rows, seen, clean = [], set(), []
    try:
        for number, raw in enumerate(reader, 1):
            if number > 500:
                raise ValueError("This demo accepts up to 500 contact rows at a time.")
            issues = []
            if None in raw or any(value is None for value in raw.values()):
                issues.append("Column count does not match the header")
            row = {key: " ".join(str(raw.get(key) or "").split()) for key in ("name", "email", "company")}
            row["email"] = row["email"].lower()
            if not row["name"]:
                issues.append("Name is missing")
            if not EMAIL.fullmatch(row["email"]):
                issues.append("Email format is invalid")
            elif row["email"] in seen:
                issues.append("Duplicate email")
            export_row = {key: spreadsheet_safe(value) for key, value in row.items()}
            escaped = export_row != row
            if not issues:
                seen.add(row["email"])
                clean.append(export_row)
            rows.append({**row, "row": number, "issues": issues, "safe_export": escaped})
    except csv.Error as error:
        raise ValueError("Check CSV quoting and separators: " + str(error)) from error
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["name", "email", "company"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(clean)
    return {
        "rows": rows,
        "total": len(rows),
        "ready": len(clean),
        "review": len(rows) - len(clean),
        "escaped": sum(row["safe_export"] and not row["issues"] for row in rows),
        "csv": output.getvalue(),
    }


def create_app(db_path=None, signing_key=None):
    path = Path(db_path) if db_path else BASE / "data" / "workflow-lab.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = signing_key or os.environ.get("WORKFLOW_SIGNING_SECRET", secrets.token_hex(32)).encode()
    if isinstance(secret, str):
        secret = secret.encode()
    today = date.today()
    sample_task = {
        "title": "Send the revised proposal to Cedar Studio",
        "owner": "Mira",
        "due_date": (today + timedelta(days=3)).isoformat(),
        "priority": "high",
    }

    @contextmanager
    def connection():
        database = sqlite3.connect(path, timeout=10)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        try:
            with database:
                yield database
        finally:
            database.close()

    def add_task(database, task, status="new"):
        result = database.execute(
            "INSERT INTO tasks(title,owner,due_date,priority,status,created_at) VALUES(?,?,?,?,?,?)",
            (task.title, task.owner, task.due_date.isoformat(), task.priority, status, now()),
        )
        return result.lastrowid

    def seed(database):
        examples = [
            ("Send the supplier brief", "Rowan", 1, "high", "new"),
            ("Review the studio onboarding", "Mira", 2, "medium", "in_progress"),
            ("Confirm the project handover", "Jules", 3, "medium", "new"),
            ("Prepare the weekly summary", "Noor", 0, "low", "done"),
        ]
        for title, owner, days, priority, status in examples:
            add_task(database, TaskInput(title=title, owner=owner, due_date=today + timedelta(days=days), priority=priority), status)

    with connection() as database:
        database.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, owner TEXT NOT NULL, due_date TEXT NOT NULL,
                priority TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                task_id INTEGER NOT NULL REFERENCES tasks(id)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
                outcome TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
        if not database.execute("SELECT 1 FROM tasks LIMIT 1").fetchone():
            seed(database)

    app = FastAPI(title="Workflow Lab", version="1.0.0", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        # shortcut: local JSON APIs require Content-Length; add bounded streaming before supporting chunked clients.
        if request.url.path.startswith("/api/") and request.method in ("POST", "PATCH"):
            length = request.headers.get("content-length")
            try:
                size = int(length) if length is not None else None
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)
            if size is None:
                return JSONResponse({"detail": "Content-Length is required."}, status_code=411)
            if size < 0 or size > MAX_BODY:
                return JSONResponse({"detail": "Keep the request below 200 KB."}, status_code=413)
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                return JSONResponse({"detail": "Send application/json."}, status_code=415)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return FileResponse(BASE / "static" / "index.html")

    @app.get("/api/state")
    def state():
        with connection() as database:
            tasks = [dict(row) for row in database.execute("SELECT * FROM tasks ORDER BY id DESC")]
            attempts = [dict(row) for row in database.execute("SELECT * FROM attempts ORDER BY id DESC LIMIT 20")]
            outcomes = dict(database.execute("SELECT outcome,COUNT(*) FROM attempts GROUP BY outcome").fetchall())
        return {
            "tasks": tasks,
            "attempts": attempts,
            "metrics": {
                "total": len(tasks),
                "new": sum(task["status"] == "new" for task in tasks),
                "in_progress": sum(task["status"] == "in_progress" for task in tasks),
                "done": sum(task["status"] == "done" for task in tasks),
                "accepted": outcomes.get("accepted", 0),
                "duplicate": outcomes.get("duplicate", 0),
                "rejected": outcomes.get("rejected", 0) + outcomes.get("conflict", 0),
            },
            "sample_task": sample_task,
        }

    @app.post("/api/intake/preview")
    def intake(body: IntakeInput):
        return preview_intake(body.message)

    @app.post("/api/tasks", status_code=201)
    def make_task(body: TaskInput):
        with connection() as database:
            task_id = add_task(database, body)
            return dict(database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    @app.patch("/api/tasks/{task_id}")
    def move_task(task_id: int, body: StatusInput):
        with connection() as database:
            result = database.execute("UPDATE tasks SET status=? WHERE id=?", (body.status, task_id))
            if not result.rowcount:
                raise HTTPException(404, "Task not found.")
            return dict(database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    @app.post("/api/import/preview")
    def import_contacts(body: ImportInput):
        try:
            return inspect_csv(body.csv_text)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    def process_event(raw, signature):
        expected = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
        valid = bool(re.fullmatch(r"sha256=[a-f0-9]{64}", signature or ""))
        if not valid or not hmac.compare_digest(expected, signature):
            with connection() as database:
                database.execute("INSERT INTO attempts(event_id,outcome,created_at) VALUES(?,?,?)", ("unverified", "rejected", now()))
            return {"http_status": 401, "outcome": "rejected", "event_id": "unverified", "message": "Signature did not match. No task was created."}
        try:
            event = EventInput.model_validate_json(raw)
        except ValidationError as error:
            raise HTTPException(422, "Use a task.requested event with an id and valid task fields.") from error
        canonical = json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        fingerprint = hashlib.sha256(canonical).hexdigest()
        with connection() as database:
            database.execute("BEGIN IMMEDIATE")
            existing = database.execute("SELECT * FROM events WHERE id=?", (event.id,)).fetchone()
            if existing:
                same = hmac.compare_digest(existing["fingerprint"], fingerprint)
                outcome, status = ("duplicate", 200) if same else ("conflict", 409)
                message = "Already received. The existing task was kept." if same else "This event ID was used for a different payload."
                task_id = existing["task_id"]
            else:
                task_id = add_task(database, event.task)
                database.execute("INSERT INTO events(id,fingerprint,task_id) VALUES(?,?,?)", (event.id, fingerprint, task_id))
                outcome, status, message = "accepted", 201, "Signature verified. One task was added to the queue."
            database.execute("INSERT INTO attempts(event_id,outcome,created_at) VALUES(?,?,?)", (event.id, outcome, now()))
        return {"http_status": status, "outcome": outcome, "event_id": event.id, "task_id": task_id, "message": message}

    @app.post("/api/webhooks")
    async def webhook(request: Request):
        result = await run_in_threadpool(process_event, await request.body(), request.headers.get("x-lab-signature"))
        return JSONResponse(result, status_code=result["http_status"])

    @app.post("/api/events/simulate")
    def simulate(body: SimulationInput):
        # shortcut: the loopback-only demo exposes a simulator; remove it before any public deployment.
        raw = json.dumps({"id": body.event_id, "type": "task.requested", "task": sample_task}, separators=(",", ":")).encode()
        signature = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
        if body.signature == "invalid":
            signature = "sha256=" + "0" * 64
        return process_event(raw, signature)

    @app.post("/api/reset")
    def reset():
        with connection() as database:
            database.execute("DELETE FROM attempts")
            database.execute("DELETE FROM events")
            database.execute("DELETE FROM tasks")
            seed(database)
        return {"message": "The example workspace was reset."}

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8787)
