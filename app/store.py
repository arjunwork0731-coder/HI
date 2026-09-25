"""Audit store: every run (including all events, evidence, verdicts and
revisions) is persisted as JSON in SQLite so decisions can be reviewed later."""
from __future__ import annotations

import json
import sqlite3
import threading

from .config import DATA_DIR

DB_PATH = DATA_DIR / "runs.sqlite3"
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, created_at TEXT, task TEXT, status TEXT, confidence REAL, mode TEXT, record TEXT)""")
    return c


def save_run(record: dict):
    res = record.get("result") or {}
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)",
                  (record["id"], record["created_at"], record["task"], res.get("status"), res.get("confidence"),
                   record.get("mode"), json.dumps(record, ensure_ascii=False, default=str)))


def get_run(run_id: str) -> dict | None:
    with _lock, _conn() as c:
        row = c.execute("SELECT record FROM runs WHERE id=?", (run_id,)).fetchone()
    return json.loads(row[0]) if row else None


def list_runs(limit: int = 50) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT id, created_at, task, status, confidence, mode FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r[0], "created_at": r[1], "task": r[2], "status": r[3], "confidence": r[4], "mode": r[5]} for r in rows]
