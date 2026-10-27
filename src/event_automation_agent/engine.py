from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workflow:
    steps: tuple[Callable[[dict], None], ...]

    def __post_init__(self):
        if not self.steps:
            raise ValueError("workflow must have at least one step")


class EventAutomation:
    def __init__(
        self,
        db_path: str | Path,
        secret: bytes,
        handlers: dict[str, Callable[[dict], None] | Workflow],
        *,
        max_attempts: int = 4,
    ):
        if len(secret) < 16:
            raise ValueError("Webhook secret must be at least 16 bytes")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.db_path, self.secret, self.handlers, self.max_attempts = (
            str(db_path),
            secret,
            dict(handlers),
            max_attempts,
        )
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, workflow TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, due_at REAL NOT NULL, error TEXT)"
            )
            if "step_index" not in {
                row[1] for row in db.execute("PRAGMA table_info(jobs)")
            }:
                db.execute(
                    "ALTER TABLE jobs ADD COLUMN step_index INTEGER NOT NULL DEFAULT 0"
                )

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def submit(
        self, event_id: str, workflow: str, body: bytes, signature: str, timestamp: int
    ) -> bool:
        if abs(time.time() - timestamp) > 300:
            raise PermissionError("Stale webhook timestamp")
        signed = str(timestamp).encode() + b"." + event_id.encode() + b"." + body
        digest = hmac.new(self.secret, signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.removeprefix("sha256="), digest):
            raise PermissionError("Invalid webhook signature")
        if workflow not in self.handlers or not event_id:
            raise ValueError("Unknown workflow or empty event ID")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise TypeError("Webhook payload must be a JSON object")
        with self._db() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO jobs(id,workflow,payload,status,due_at) VALUES(?,?,?,'queued',?)",
                (event_id, workflow, json.dumps(payload), time.time()),
            )
        return cursor.rowcount == 1

    def process_one(self, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE jobs SET status='queued' WHERE status='running' AND due_at<=?",
                (now,),
            )
            row = db.execute(
                "SELECT id,workflow,payload,attempts,step_index FROM jobs WHERE status='queued' AND due_at<=? ORDER BY due_at,id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return False
            event_id, workflow, payload, attempts, step_index = row
            db.execute(
                "UPDATE jobs SET status='running',attempts=attempts+1,due_at=? WHERE id=?",
                (now + 300, event_id),
            )
        try:
            registered = self.handlers[workflow]
            steps = (
                registered.steps if isinstance(registered, Workflow) else (registered,)
            )
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise TypeError("event payload must be a JSON object")
            event["_event_id"] = event_id
            steps[step_index](event)
        except Exception as exc:  # noqa: BLE001 - handler failure must enter retry queue
            attempts += 1
            status = "dead" if attempts >= self.max_attempts else "queued"
            with self._db() as db:
                db.execute(
                    "UPDATE jobs SET status=?,due_at=?,error=? WHERE id=?",
                    (status, now + min(2**attempts, 300), str(exc)[:500], event_id),
                )
        else:
            with self._db() as db:
                complete = step_index + 1 >= len(steps)
                db.execute(
                    "UPDATE jobs SET status=?,step_index=step_index+1,attempts=0,due_at=?,error=NULL WHERE id=?",
                    ("completed" if complete else "queued", now, event_id),
                )
        return True

    def requeue_dead(self, event_id: str) -> bool:
        with self._db() as db:
            return (
                db.execute(
                    "UPDATE jobs SET status='queued',attempts=0,due_at=?,error=NULL WHERE id=? AND status='dead'",
                    (time.time(), event_id),
                ).rowcount
                == 1
            )

    def counts(self) -> dict[str, int]:
        with self._db() as db:
            return dict(
                db.execute(
                    "SELECT status,count(*) FROM jobs GROUP BY status"
                ).fetchall()
            )

    def job(self, event_id: str) -> tuple[str, int, int] | None:
        with self._db() as db:
            return db.execute(
                "SELECT status,step_index,attempts FROM jobs WHERE id=?", (event_id,)
            ).fetchone()
