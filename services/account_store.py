"""Account-owned settings, memories and a durable new-message extraction queue.

SQLite is the source of truth. Memory vectors are disposable retrieval hints;
no personal text is added to the shared school knowledge collection.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from config.llm import DEFAULT_CHAT_MODEL


# chat_model：輸入框「⋯」選單選擇的華為雲 MaaS 模型（不在「設定-通用」顯示）
DEFAULT_SETTINGS = {"theme": "light", "auto_practice": True, "auto_web_search": True, "auto_think": True,
                    "auto_memory": True, "use_memory": True, "chat_model": DEFAULT_CHAT_MODEL}
CATEGORIES = {"preference", "goal", "background", "learning"}


def now():
    return datetime.now(timezone.utc).isoformat()


def normalized(value):
    # Chinese scripts are normalized before content-based duplicate detection.
    import zhconv
    text = zhconv.convert(unicodedata.normalize("NFKC", str(value)), "zh-cn").lower()
    return re.sub(r"[\W_]+", "", text)


def content_key(value):
    return hashlib.sha256(normalized(value).encode("utf-8")).hexdigest()


class AccountStore:
    def __init__(self, root):
        self.path = Path(root) / "storage" / "accounts.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account_settings (
                    owner TEXT PRIMARY KEY, settings TEXT NOT NULL,
                    activated_at TEXT NOT NULL, epoch INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, content TEXT NOT NULL,
                    category TEXT NOT NULL, content_key TEXT NOT NULL, fact_key TEXT NOT NULL,
                    conversation_id TEXT, message_id TEXT, reply_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    vector_model TEXT, vector TEXT,
                    UNIQUE(owner,content_key));
                CREATE INDEX IF NOT EXISTS memories_owner ON memories(owner,updated_at);
                CREATE TABLE IF NOT EXISTS memory_tombstones (
                    owner TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY(owner,key));
                CREATE TABLE IF NOT EXISTS memory_jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, reply_id TEXT NOT NULL, question TEXT NOT NULL,
                    epoch INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
                    available_at REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, UNIQUE(owner,message_id));
                CREATE INDEX IF NOT EXISTS memory_jobs_pending ON memory_jobs(status,available_at);
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

    def _account(self, db, owner):
        db.execute("INSERT OR IGNORE INTO account_settings VALUES(?,?,?,0)",
                   (owner, json.dumps(DEFAULT_SETTINGS), now()))
        return db.execute("SELECT * FROM account_settings WHERE owner=?", (owner,)).fetchone()

    @staticmethod
    def _settings(row):
        stored = json.loads(row["settings"])
        values = {key: stored.get(key, value) for key, value in DEFAULT_SETTINGS.items()}
        if row["owner"] == "0000":
            values.update(auto_memory=False, use_memory=False)
        return values

    def settings(self, owner):
        with self.lock, self.connect() as db:
            return self._settings(self._account(db, owner))

    def theme_initialized(self, owner):
        with self.lock, self.connect() as db:
            stored = json.loads(self._account(db, owner)["settings"])
            return bool(stored.get("_theme_initialized") or stored.get("theme") == "dark")

    def patch_settings(self, owner, changes):
        with self.lock, self.connect() as db:
            row = self._account(db, owner)
            settings = self._settings(row)
            values = {**settings, **changes}
            if owner == "0000":
                values.update(auto_memory=False, use_memory=False)
            invalidate = settings["auto_memory"] and not values["auto_memory"]
            private = {**values, "_theme_initialized": "theme" in changes or bool(json.loads(row["settings"]).get("_theme_initialized"))}
            db.execute("UPDATE account_settings SET settings=?,epoch=epoch+? WHERE owner=?",
                       (json.dumps(private), int(invalidate), owner))
            if invalidate:
                db.execute("UPDATE memory_jobs SET status='cancelled',question='',updated_at=? "
                           "WHERE owner=? AND status IN ('queued','running')", (now(), owner))
            return values

    @staticmethod
    def _memory(row):
        if row is None:
            return None
        value = dict(row)
        value["vector"] = json.loads(value["vector"]) if value.get("vector") else None
        return value

    def memories(self, owner):
        with self.connect() as db:
            return [self._memory(r) for r in db.execute(
                "SELECT * FROM memories WHERE owner=? ORDER BY updated_at DESC,id", (owner,))]

    def memory(self, owner, memory_id):
        with self.connect() as db:
            return self._memory(db.execute("SELECT * FROM memories WHERE owner=? AND id=?",
                                          (owner, memory_id)).fetchone())

    def _put(self, db, owner, content, category, *, fact_key="", source=None,
             vector=None, vector_model=None, allow_tombstone=False):
        key = content_key(content)
        semantic_key = "fact:" + fact_key if fact_key else ""
        blocked = db.execute("SELECT 1 FROM memory_tombstones WHERE owner=? AND key IN (?,?)",
                             (owner, key, semantic_key)).fetchone()
        if blocked and not allow_tombstone:
            return None
        if allow_tombstone:
            db.execute("DELETE FROM memory_tombstones WHERE owner=? AND key IN (?,?)",
                       (owner, key, semantic_key))
        existing = db.execute("SELECT * FROM memories WHERE owner=? AND content_key=?", (owner, key)).fetchone()
        if not existing and fact_key:
            existing = db.execute("SELECT * FROM memories WHERE owner=? AND fact_key=?",
                                  (owner, fact_key)).fetchone()
        source = source or {}
        if existing and existing["message_id"] and source.get("message_id"):
            previous = db.execute("SELECT created_at FROM memory_jobs WHERE owner=? AND message_id=?",
                                  (owner, existing["message_id"])).fetchone()
            current = db.execute("SELECT created_at FROM memory_jobs WHERE owner=? AND message_id=?",
                                 (owner, source["message_id"])).fetchone()
            if previous and current and previous["created_at"] > current["created_at"]:
                # A delayed retry of an old message cannot reverse a newer correction.
                return None
        stamp = now()
        memory_id, created = (existing["id"], existing["created_at"]) if existing else (uuid4().hex, stamp)
        db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                   "content=excluded.content,category=excluded.category,content_key=excluded.content_key,"
                   "fact_key=excluded.fact_key,conversation_id=excluded.conversation_id,message_id=excluded.message_id,"
                   "reply_id=excluded.reply_id,updated_at=excluded.updated_at,vector_model=excluded.vector_model,vector=excluded.vector",
                   (memory_id, owner, content, category, key, fact_key, source.get("conversation_id"),
                    source.get("message_id"), source.get("reply_id"), created, stamp, vector_model,
                    json.dumps(vector) if vector is not None else None))
        return self._memory(db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())

    def add(self, owner, content, category, **kwargs):
        with self.lock, self.connect() as db:
            self._invalidate(db, owner)
            return self._put(db, owner, content, category, allow_tombstone=True, **kwargs)

    def edit(self, owner, memory_id, changes, *, vector=None, vector_model=None):
        with self.lock, self.connect() as db:
            existing = db.execute("SELECT * FROM memories WHERE owner=? AND id=?", (owner, memory_id)).fetchone()
            if not existing:
                return None
            content = changes.get("content", existing["content"])
            key = content_key(content)
            other = db.execute("SELECT id FROM memories WHERE owner=? AND content_key=? AND id!=?",
                               (owner, key, memory_id)).fetchone()
            if other:
                raise ValueError("已有相同內容的記憶")
            # A user's edit wins over an extraction that was already in progress.
            self._invalidate(db, owner)
            self._tombstone(db, owner, existing)
            db.execute("UPDATE memories SET content=?,category=?,content_key=?,fact_key='',updated_at=?,"
                       "vector=?,vector_model=? WHERE owner=? AND id=?",
                       (content, changes.get("category", existing["category"]), key, now(),
                        json.dumps(vector) if vector is not None else None, vector_model, owner, memory_id))
            return self._memory(db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())

    @staticmethod
    def _tombstone(db, owner, row):
        keys = [row["content_key"]]
        if row["fact_key"]:
            keys.append("fact:" + row["fact_key"])
        db.executemany("INSERT OR IGNORE INTO memory_tombstones VALUES(?,?)", [(owner, k) for k in keys])

    def _invalidate(self, db, owner):
        self._account(db, owner)
        db.execute("UPDATE account_settings SET epoch=epoch+1 WHERE owner=?", (owner,))
        db.execute("UPDATE memory_jobs SET status='cancelled',question='',updated_at=? "
                   "WHERE owner=? AND status IN ('queued','running')", (now(), owner))

    def delete(self, owner, memory_id):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE owner=? AND id=?", (owner, memory_id)).fetchone()
            if not row:
                return False
            self._invalidate(db, owner)
            self._tombstone(db, owner, row)
            db.execute("DELETE FROM memories WHERE owner=? AND id=?", (owner, memory_id))
            return True

    def clear(self, owner, category=None):
        with self.lock, self.connect() as db:
            self._invalidate(db, owner)
            rows = db.execute("SELECT * FROM memories WHERE owner=?" + (" AND category=?" if category else ""),
                              (owner, category) if category else (owner,)).fetchall()
            for row in rows:
                self._tombstone(db, owner, row)
            db.execute("DELETE FROM memories WHERE owner=?" + (" AND category=?" if category else ""),
                       (owner, category) if category else (owner,))
            return len(rows)

    def enqueue(self, owner, conversation_id, message_id, reply_id, question):
        with self.lock, self.connect() as db:
            account = self._account(db, owner)
            if not self._settings(account)["auto_memory"]:
                return None
            existing = db.execute("SELECT * FROM memory_jobs WHERE owner=? AND message_id=?",
                                  (owner, message_id)).fetchone()
            if existing:
                return dict(existing)
            job_id, stamp = uuid4().hex, now()
            db.execute("INSERT INTO memory_jobs VALUES(?,?,?,?,?,?,?,'queued',0,0,?,?)",
                       (job_id, owner, conversation_id, message_id, reply_id, question, account["epoch"], stamp, stamp))
            return dict(db.execute("SELECT * FROM memory_jobs WHERE id=?", (job_id,)).fetchone())

    def claim(self, job_id):
        with self.lock, self.connect() as db:
            job = db.execute("SELECT * FROM memory_jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] != "queued" or job["attempts"] >= 3:
                return None
            account = self._account(db, job["owner"])
            if account["epoch"] != job["epoch"] or not self._settings(account)["auto_memory"]:
                db.execute("UPDATE memory_jobs SET status='cancelled',question='',updated_at=? WHERE id=?", (now(), job_id))
                return None
            db.execute("UPDATE memory_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=?", (now(), job_id))
            return dict(db.execute("SELECT * FROM memory_jobs WHERE id=?", (job_id,)).fetchone())

    def complete(self, job_id, candidates):
        with self.lock, self.connect() as db:
            job = db.execute("SELECT * FROM memory_jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] != "running":
                return []
            account = self._account(db, job["owner"])
            if account["epoch"] != job["epoch"] or not self._settings(account)["auto_memory"]:
                db.execute("UPDATE memory_jobs SET status='cancelled',question='',updated_at=? WHERE id=?", (now(), job_id))
                return []
            result = []
            source = {"conversation_id": job["conversation_id"], "message_id": job["message_id"], "reply_id": job["reply_id"]}
            for candidate in candidates[:3]:
                memory = self._put(db, job["owner"], source=source, **candidate)
                if memory:
                    result.append(memory)
            db.execute("UPDATE memory_jobs SET status='succeeded',question='',updated_at=? WHERE id=?", (now(), job_id))
            return result

    def finish(self, job_id, status="skipped", available_at=0):
        with self.lock, self.connect() as db:
            # User deletion/clear has precedence over a late worker's completion.
            db.execute("UPDATE memory_jobs SET status=?,available_at=?,question=CASE WHEN ?='queued' THEN question ELSE '' END,"
                       "updated_at=? WHERE id=? AND status IN ('running','queued')",
                       (status, available_at, status, now(), job_id))

    def recover(self):
        with self.lock, self.connect() as db:
            db.execute("UPDATE memory_jobs SET status='queued',updated_at=? WHERE status='running' AND attempts<3", (now(),))
            db.execute("UPDATE memory_jobs SET status='failed',question='',updated_at=? WHERE status='running' AND attempts>=3", (now(),))
            return [dict(row) for row in db.execute("SELECT * FROM memory_jobs WHERE status='queued' AND attempts<3 ORDER BY created_at")]

    def cancel_conversation(self, owner, conversation_id):
        with self.lock, self.connect() as db:
            db.execute("UPDATE memory_jobs SET status='cancelled',question='',updated_at=? "
                       "WHERE owner=? AND conversation_id=? AND status IN ('queued','running')", (now(), owner, conversation_id))

    def job_summary(self, owner, job_id):
        with self.connect() as db:
            row = db.execute("SELECT id,status,message_id FROM memory_jobs WHERE owner=? AND id=?", (owner, job_id)).fetchone()
            if not row:
                return None
            count = db.execute("SELECT COUNT(*) FROM memories WHERE owner=? AND message_id=?",
                               (owner, row["message_id"])).fetchone()[0] if row["status"] == "succeeded" else 0
            return {"job_id": row["id"], "status": row["status"], "saved_count": count}
