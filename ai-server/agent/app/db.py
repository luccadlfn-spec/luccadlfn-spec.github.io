"""SQLite storage: conversations, tasks, schedules, notes (FTS5) and a key-value table.

One connection, WAL mode. Every statement is short, so calls are made directly
from the event loop; the lock only matters if a helper thread ever touches it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import Any

INTERACTIVE_KINDS = ("chat", "api", "telegram", "webui")
TASK_KINDS = INTERACTIVE_KINDS + ("scheduled",)
FINAL_STATES = ("done", "error", "cancelled")

MAX_NOTES = 2000
MAX_NOTE_CHARS = 2000
MAX_MESSAGES_PER_CONVERSATION = 200
KEEP_FINISHED_TASKS = 500
MAX_RESULT_CHARS = 20_000
INTERRUPTED = "interrupted by restart"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, prompt TEXT NOT NULL,
    status TEXT NOT NULL, result TEXT, error TEXT,
    created_at REAL NOT NULL, started_at REAL, finished_at REAL,
    schedule_id INTEGER, steps TEXT NOT NULL DEFAULT '[]',
    background INTEGER NOT NULL DEFAULT 0, name TEXT,
    conversation_id INTEGER, reply_to INTEGER);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, id);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, spec TEXT NOT NULL,
    kind TEXT NOT NULL, cron TEXT, prompt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, next_run REAL, last_run REAL,
    created_at REAL NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5(text, created_at UNINDEXED);
"""


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _task(row: sqlite3.Row | None, full: bool = True) -> dict[str, Any] | None:
    if row is None:
        return None
    t = dict(row)
    t["background"] = bool(t["background"])
    try:
        t["steps"] = json.loads(t.get("steps") or "[]")
    except ValueError:
        t["steps"] = []
    if not full:
        t.pop("steps")
        for key in ("prompt", "result", "error"):
            if t.get(key) and len(t[key]) > 300:
                t[key] = t[key][:300] + "…"
    return t


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._c = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self._c.row_factory = sqlite3.Row
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA synchronous=NORMAL")
        self._c.execute("PRAGMA foreign_keys=ON")
        self._c.execute("PRAGMA busy_timeout=5000")
        self._c.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._c.close()

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._c.execute(sql, args)

    # ---- key-value ----
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self._x("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def kv_set(self, key: str, value: Any) -> None:
        self._x("INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    # ---- conversations ----
    def create_conversation(self, channel: str) -> int:
        now = time.time()
        return int(self._x("INSERT INTO conversations(channel, created_at, updated_at) VALUES(?,?,?)",
                           (channel, now, now)).lastrowid)

    def conversation_exists(self, conversation_id: int) -> bool:
        return self._x("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone() is not None

    def add_message(self, conversation_id: int, role: str, content: str) -> None:
        now = time.time()
        with self._lock:
            self._x("INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)",
                    (conversation_id, role, content, now))
            self._x("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
            # Bounded history: the budget trims what is sent, this trims what is kept.
            self._x("DELETE FROM messages WHERE conversation_id=? AND id NOT IN ("
                    "SELECT id FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?)",
                    (conversation_id, conversation_id, MAX_MESSAGES_PER_CONVERSATION))

    def get_messages(self, conversation_id: int) -> list[dict[str, str]]:
        rows = self._x("SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id",
                       (conversation_id,)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def prune_conversations(self, max_age_days: int = 90) -> None:
        self._x("DELETE FROM conversations WHERE updated_at < ?", (time.time() - max_age_days * 86400,))

    # ---- tasks ----
    def create_task(self, kind: str, prompt: str, *, status: str = "queued", background: bool = False,
                    schedule_id: int | None = None, name: str | None = None,
                    conversation_id: int | None = None, reply_to: int | None = None) -> int:
        if kind not in TASK_KINDS:
            raise ValueError(f"unknown task kind {kind!r}")
        now = time.time()
        started = now if status == "running" else None
        return int(self._x(
            "INSERT INTO tasks(kind, prompt, status, created_at, started_at, schedule_id, background,"
            " name, conversation_id, reply_to) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (kind, prompt, status, now, started, schedule_id, int(background), name,
             conversation_id, reply_to)).lastrowid)

    def claim_next_task(self, include_background: bool) -> dict[str, Any] | None:
        """Mark the next queued task as running. Interactive tasks always go first."""
        with self._lock:
            row = self._x(
                "SELECT id FROM tasks WHERE status='queued'" +
                ("" if include_background else " AND background=0") +
                " ORDER BY background, id LIMIT 1").fetchone()
            if row is None:
                return None
            self._x("UPDATE tasks SET status='running', started_at=? WHERE id=?", (time.time(), row["id"]))
            return self.get_task(row["id"])

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        return _task(self._x("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._x("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (max(1, min(100, limit)),)).fetchall()
        return [_task(r, full=False) for r in rows]

    def update_steps(self, task_id: int, steps: list[dict]) -> None:
        self._x("UPDATE tasks SET steps=? WHERE id=?", (json.dumps(steps, ensure_ascii=False), task_id))

    def finish_task(self, task_id: int, status: str, *, result: str | None = None,
                    error: str | None = None, steps: list[dict] | None = None) -> None:
        if status not in FINAL_STATES:
            raise ValueError(f"not a final task state: {status!r}")
        with self._lock:
            if steps is not None:
                self.update_steps(task_id, steps)
            self._x("UPDATE tasks SET status=?, result=?, error=?, finished_at=? WHERE id=?",
                    (status, (result or "")[:MAX_RESULT_CHARS] or None,
                     (error or "")[:2000] or None, time.time(), task_id))
            self._x("DELETE FROM tasks WHERE status IN ('done','error','cancelled') AND id NOT IN ("
                    "SELECT id FROM tasks WHERE status IN ('done','error','cancelled')"
                    " ORDER BY id DESC LIMIT ?)", (KEEP_FINISHED_TASKS,))

    def cancel_queued_task(self, task_id: int) -> bool:
        cur = self._x("UPDATE tasks SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
                      (time.time(), task_id))
        return cur.rowcount > 0

    def mark_interrupted(self) -> list[dict[str, Any]]:
        """Tasks that were running when the process stopped cannot be resumed. Returns them."""
        with self._lock:
            ids = [r["id"] for r in self._x("SELECT id FROM tasks WHERE status='running'").fetchall()]
            self._x("UPDATE tasks SET status='error', error=?, finished_at=? WHERE status='running'",
                    (INTERRUPTED, time.time()))
            return [t for t in (self.get_task(i) for i in ids) if t is not None]

    def count_interrupted(self, schedule_id: int) -> int:
        return int(self._x("SELECT COUNT(*) AS n FROM tasks WHERE schedule_id=? AND error=?",
                           (schedule_id, INTERRUPTED)).fetchone()["n"])

    def count_tasks(self, status: str) -> int:
        return int(self._x("SELECT COUNT(*) AS n FROM tasks WHERE status=?", (status,)).fetchone()["n"])

    def schedule_has_open_task(self, schedule_id: int) -> bool:
        return self._x("SELECT 1 FROM tasks WHERE schedule_id=? AND status IN ('queued','running') LIMIT 1",
                       (schedule_id,)).fetchone() is not None

    # ---- schedules ----
    def add_schedule(self, name: str, spec: str, kind: str, cron: str | None, prompt: str,
                     next_run: float) -> int:
        return int(self._x(
            "INSERT INTO schedules(name, spec, kind, cron, prompt, enabled, next_run, created_at)"
            " VALUES(?,?,?,?,?,1,?,?)", (name, spec, kind, cron, prompt, next_run, time.time())).lastrowid)

    def list_schedules(self) -> list[dict[str, Any]]:
        rows = self._x("SELECT * FROM schedules ORDER BY id").fetchall()
        return [dict(r, enabled=bool(r["enabled"])) for r in rows]

    def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        row = self._x("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        return dict(row, enabled=bool(row["enabled"])) if row else None

    def count_schedules(self) -> int:
        return int(self._x("SELECT COUNT(*) AS n FROM schedules").fetchone()["n"])

    def update_schedule(self, schedule_id: int, **fields: Any) -> bool:
        allowed = {"enabled", "next_run", "last_run"}
        if not fields or set(fields) - allowed:
            raise ValueError("unsupported schedule fields")
        sets = ", ".join(f"{k}=?" for k in fields)
        cur = self._x(f"UPDATE schedules SET {sets} WHERE id=?", (*fields.values(), schedule_id))
        return cur.rowcount > 0

    def delete_schedule(self, schedule_id: int) -> bool:
        return self._x("DELETE FROM schedules WHERE id=?", (schedule_id,)).rowcount > 0

    # ---- notes ----
    def add_note(self, text: str) -> int:
        text = text.strip()[:MAX_NOTE_CHARS]
        with self._lock:
            note_id = int(self._x("INSERT INTO notes(text, created_at) VALUES(?, ?)",
                                  (text, time.time())).lastrowid)
            self._x("DELETE FROM notes WHERE rowid <= ?", (note_id - MAX_NOTES,))
        return note_id

    def list_notes(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._x("SELECT rowid AS id, text, created_at FROM notes ORDER BY rowid DESC LIMIT ?",
                       (max(1, min(500, limit)),)).fetchall()
        return [dict(r) for r in rows]

    def search_notes(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Best matches for free text. Words are quoted so FTS syntax in the query is inert."""
        words = re.findall(r"\w+", query, flags=re.UNICODE)[:12]
        if not words:
            return self.list_notes(limit)
        match = " OR ".join(f'"{w}"*' for w in words)
        rows = self._x("SELECT rowid AS id, text, created_at FROM notes WHERE notes MATCH ?"
                       " ORDER BY bm25(notes) LIMIT ?", (match, limit)).fetchall()
        return [dict(r) for r in rows]

    def delete_note(self, note_id: int) -> bool:
        return self._x("DELETE FROM notes WHERE rowid=?", (note_id,)).rowcount > 0
