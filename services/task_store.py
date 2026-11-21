"""Durable, account-owned background tasks. No expiry of completed results."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


class LearningDataCleared(ValueError):
    """An in-flight save belongs to a removed learning-data generation."""


class TaskStore:
    def __init__(self, root):
        self.root = Path(root) / "storage" / "background_tasks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root.parent / "background_tasks.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
                    request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, data TEXT NOT NULL,
                    UNIQUE(owner,kind,request_id));
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_task ON attempts(task_id);
                CREATE TABLE IF NOT EXISTS tutor_state (
                    owner TEXT NOT NULL, conversation_id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(owner,conversation_id));
                CREATE TABLE IF NOT EXISTS learning_epochs (
                    owner TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, owner, kind, request_id, fingerprint, payload, name, metadata=None):
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE owner=? AND kind=? AND request_id=?",
                             (owner, kind, request_id)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("同一請求編號的內容不一致")
                return json.loads(row["data"]), False
            task = dict(id=uuid4().hex, owner=owner, kind=kind, request_id=request_id,
                        name=name, status="queued", stage="等待處理", completed=0, total=None,
                        unit="", revision=1, created_at=now(), updated_at=now(), seen=False,
                        payload=payload, result=None, error=None, draft={}, touched_fields=[])
            task.update({k: v for k, v in (metadata or {}).items()
                         if k in {"origin", "conversation_id", "message_id", "skipped", "generation_attempts"}})
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", (task["id"], owner, kind,
                       request_id, fingerprint, json.dumps(task, ensure_ascii=False)))
            return task, True

    def get(self, task_id, owner=None):
        with self.connect() as db:
            row = db.execute("SELECT owner,data FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or (owner is not None and row["owner"] != owner):
                return None
            return json.loads(row["data"])

    def list(self, owner):
        with self.connect() as db:
            rows = db.execute("SELECT data FROM tasks WHERE owner=? ORDER BY rowid DESC", (owner,)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def chat_tasks(self):
        with self.connect() as db:
            return [task for row in db.execute("SELECT data FROM tasks WHERE kind='quiz'").fetchall()
                    if (task := json.loads(row[0])).get("origin") == "chat"]

    def tutor(self, owner, conversation_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM tutor_state WHERE owner=? AND conversation_id=?",
                             (owner, conversation_id)).fetchone()
            return json.loads(row[0]) if row else {"turns": 2, "seen": [], "active_task": None}

    def tutor_turn(self, owner, conversation_id, message_id, *, pending=False, manual=False):
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM tutor_state WHERE owner=? AND conversation_id=?",
                             (owner, conversation_id)).fetchone()
            state = json.loads(row[0]) if row else {"turns": 2, "seen": [], "active_task": None}
            if message_id in state["seen"]:
                return False
            state["seen"] = (state["seen"] + [message_id])[-50:]
            state["turns"] = min(3, state["turns"] + 1)
            offer = manual or (not pending and state["turns"] >= 3)
            if offer:
                state["turns"] = 0
            db.execute("INSERT INTO tutor_state VALUES(?,?,?) ON CONFLICT(owner,conversation_id) DO UPDATE SET data=excluded.data",
                       (owner, conversation_id, json.dumps(state, ensure_ascii=False)))
            return offer

    def tutor_update(self, owner, conversation_id, **changes):
        with self.lock:
            state = self.tutor(owner, conversation_id)
            state.update(changes)
            with self.connect() as db:
                db.execute("INSERT INTO tutor_state VALUES(?,?,?) ON CONFLICT(owner,conversation_id) DO UPDATE SET data=excluded.data",
                           (owner, conversation_id, json.dumps(state, ensure_ascii=False)))
            return state

    def learning_epoch(self, owner):
        with self.connect() as db:
            row = db.execute("SELECT epoch FROM learning_epochs WHERE owner=?", (owner,)).fetchone()
            return row[0] if row else 0

    def clear_attempts(self, owner):
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO learning_epochs VALUES(?,1) ON CONFLICT(owner) DO UPDATE SET epoch=epoch+1", (owner,))
            db.execute("DELETE FROM attempts WHERE task_id IN (SELECT id FROM tasks WHERE owner=?)", (owner,))

    def remove_tutor(self, owner, conversation_id):
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM tutor_state WHERE owner=? AND conversation_id=?", (owner, conversation_id))

    def update(self, task_id, **changes):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            task = json.loads(row[0])
            task.update(changes)
            task.update(revision=task["revision"] + 1, updated_at=now())
            db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task, ensure_ascii=False), task_id))
            return task

    def draft(self, task_id, values, touched, client_id=None, sequence=None):
        with self.lock:
            task = self.get(task_id)
            clients = dict(task.get("draft_clients", {}))
            if client_id:
                if sequence <= clients.get(client_id, -1):
                    return task
                clients[client_id] = sequence
            draft = {**task["draft"], **values}
            fields = sorted(set(task["touched_fields"]) | set(touched))
            return self.update(task_id, draft=draft, touched_fields=fields, draft_clients=clients)

    def merge_suggestions(self, task_id, values):
        with self.lock:
            task = self.get(task_id)
            draft = dict(task["draft"])
            protected = set(task["touched_fields"])
            for pair in ({"subject", "subject_custom"}, {"material_type", "material_custom"}):
                if protected & pair:
                    protected.update(pair)
            draft.update({k: v for k, v in values.items() if k not in protected})
            return self.update(task_id, draft=draft)

    def recover(self):
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT id,data FROM tasks").fetchall()
            for row in rows:
                task = json.loads(row["data"])
                if task["status"] in {"running", "queued"}:
                    task.update(status="interrupted", stage="服務重啟，請重試", error="處理已中斷，已完成內容仍保留",
                                updated_at=now(), revision=task["revision"] + 1)
                    db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task, ensure_ascii=False), row["id"]))

    def expire_drafts(self):
        """Expire only private uncommitted originals; retain every task entry."""
        with self.lock:
            with self.connect() as db:
                tasks = [json.loads(row[0]) for row in db.execute("SELECT data FROM tasks").fetchall()]
            for task in tasks:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(task["created_at"])
                if task["kind"] != "upload" or task.get("committed_file_id") or task["status"] in {"succeeded", "running", "queued"} or age.days < 7:
                    continue
                original = self.root / task["id"] / "original"
                if original.is_file():
                    original.unlink()
                    self.update(task["id"], status="interrupted", stage="暫存原件已過期", extraction=None,
                                error="請重新選擇檔案；任務和分類記錄仍保留")

    def save_attempt(self, task_id, attempt):
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT owner FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise LearningDataCleared("學習記錄已移除，請重新開始")
            epoch = db.execute("SELECT epoch FROM learning_epochs WHERE owner=?", (task[0],)).fetchone()
            current = epoch[0] if epoch else 0
            if attempt.get("learning_epoch", 0) != current:
                raise LearningDataCleared("學習記錄已清除，請重新開始")
            attempt["learning_epoch"] = current
            db.execute("INSERT INTO attempts VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                       (attempt["id"], task_id, json.dumps(attempt, ensure_ascii=False)))
        return attempt

    def attempt(self, task_id, attempt_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM attempts WHERE task_id=? AND id=?", (task_id, attempt_id)).fetchone()
            return json.loads(row[0]) if row else None

    def attempts(self, task_id):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT data FROM attempts WHERE task_id=? ORDER BY rowid DESC", (task_id,)).fetchall()]

    def delete(self, task_id):
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM attempts WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
