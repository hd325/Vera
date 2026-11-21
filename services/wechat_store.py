"""Small durable WeChat control plane; never an alternative chat database."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class WechatError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


class WechatStore:
    def __init__(self, root):
        self.path = Path(root) / "storage" / "wechat.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bindings(owner TEXT PRIMARY KEY,account_id TEXT NOT NULL,
                  sender_id TEXT NOT NULL,state TEXT NOT NULL,epoch INTEGER NOT NULL,data TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS wechat_identity ON bindings(account_id,sender_id) WHERE state='connected';
                CREATE TABLE IF NOT EXISTS bind_requests(id TEXT PRIMARY KEY,owner TEXT NOT NULL,state TEXT NOT NULL,
                  expires REAL NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY,owner TEXT NOT NULL,conversation_id TEXT NOT NULL,
                  event_key TEXT UNIQUE NOT NULL,status TEXT NOT NULL,epoch INTEGER NOT NULL,
                  cap_hash TEXT NOT NULL,expires REAL NOT NULL,data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS wechat_turn_owner ON turns(owner,status);
                CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,turn_id TEXT UNIQUE NOT NULL,
                  owner TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wechat_events(event_key TEXT PRIMARY KEY,turn_id TEXT NOT NULL,fingerprint TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wechat_tool_calls(turn_id TEXT NOT NULL,call_id TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(turn_id,call_id));
                CREATE TABLE IF NOT EXISTS wechat_controls(event_key TEXT PRIMARY KEY,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS native_commits(advancement_key TEXT PRIMARY KEY,
                  session_hash TEXT NOT NULL,admission_hash TEXT NOT NULL,terminal_hash TEXT NOT NULL,
                  payload_hash TEXT NOT NULL,created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS native_runs(run_id TEXT PRIMARY KEY,turn_id TEXT NOT NULL,
                  attempt INTEGER NOT NULL,epoch INTEGER NOT NULL,cap_hash TEXT NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        return json.loads(row["data"]) if row else None

    def native_commit(self, payload):
        """Acknowledge native transcript advancement without touching business turns.

        OpenClaw may replay this record before a new run assembles its context.
        Its key and hashes therefore deliberately carry no current turn authority.
        """
        hash_fields = ("session_hash", "admission_hash", "terminal_hash", "payload_hash")
        if not isinstance(payload, dict) or set(payload) != {"advancement_key", *hash_fields}:
            raise WechatError("原生推進記錄欄位無效。", 422)
        key = payload["advancement_key"]
        if not isinstance(key, str) or not 0 < len(key) <= 512:
            raise WechatError("原生推進編號無效。", 422)
        if any(not isinstance(payload[name], str) or not re.fullmatch(r"[a-f0-9]{64}", payload[name]) for name in hash_fields):
            raise WechatError("原生推進記錄只接受內容雜湊。", 422)
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM native_commits WHERE advancement_key=?", (key,)).fetchone()
            if existing:
                if any(existing[name] != payload[name] for name in hash_fields):
                    raise WechatError("同一原生推進編號的雜湊不一致。", 409)
                return {"duplicate": True}
            db.execute("INSERT INTO native_commits VALUES(?,?,?,?,?,?)",
                       (key, *(payload[name] for name in hash_fields), time.time()))
            return {"duplicate": False}

    def native_run_start(self, run_id, turn_id, attempt, epoch, cap_hash):
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM native_runs WHERE run_id=?", (run_id,)).fetchone()
            if row:
                if row["turn_id"] != turn_id or row["attempt"] != attempt or row["epoch"] != epoch or row["cap_hash"] != cap_hash:
                    raise WechatError("原生執行編號已綁定其他回合或執行批次。", 409)
                if row["state"] == "completed":
                    return self.decode(row)
            value = {"run_id": run_id, "turn_id": turn_id, "attempt": attempt, "epoch": epoch,
                     "cap_hash": cap_hash, "state": "waiting", "output_seen": False, "end_seen": False,
                     "output_hashes": [], "terminal_text": None, "success": None, "created_at": time.time()}
            db.execute("INSERT INTO native_runs VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET state=excluded.state,data=excluded.data",
                       (run_id, turn_id, attempt, epoch, cap_hash, "waiting", json.dumps(value)))
            return value

    def native_run(self, run_id):
        with self.db() as db:
            value = self.decode(db.execute("SELECT data FROM native_runs WHERE run_id=?", (run_id,)).fetchone())
            if not value:
                raise WechatError("未找到已授權的原生執行。", 404)
            return value

    def update_native_run(self, run_id, **fields):
        allowed = {"state", "output_seen", "end_seen", "output_hashes", "terminal_text", "success", "provider", "model", "updated_at"}
        with self.lock, self.db() as db:
            value = self.decode(db.execute("SELECT data FROM native_runs WHERE run_id=?", (run_id,)).fetchone())
            if not value:
                raise WechatError("未找到已授權的原生執行。", 404)
            value.update({key: item for key, item in fields.items() if key in allowed})
            value["updated_at"] = time.time()
            db.execute("UPDATE native_runs SET state=?,data=? WHERE run_id=?", (value["state"], json.dumps(value), run_id))
            return value

    def binding(self, owner):
        with self.db() as db:
            return self.decode(db.execute("SELECT data FROM bindings WHERE owner=?", (owner,)).fetchone())

    def identity(self, account_id, sender_id):
        with self.db() as db:
            return self.decode(db.execute("SELECT data FROM bindings WHERE account_id=? AND sender_id=? AND state='connected'",
                                          (account_id, sender_id)).fetchone())

    def new_request(self, owner, lifetime=300):
        if owner == "0000":
            raise WechatError("展示帳號不能綁定私人微信。", 403)
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM bindings WHERE owner=?", (owner,)).fetchone()
            if row and row["state"] == "connected":
                raise WechatError("請先解除目前的微信連接。")
            count = db.execute("SELECT COUNT(*) FROM bindings WHERE state='connected'").fetchone()[0]
            if count >= 3:
                raise WechatError("本機試用最多連接三個獨立帳號。")
            for old in db.execute("SELECT * FROM bind_requests WHERE owner=? AND state NOT IN ('confirmed','cancelled','expired','failed','conflict')", (owner,)).fetchall():
                value = self.decode(old)
                value["state"] = "cancelled"
                db.execute("UPDATE bind_requests SET state='cancelled',data=? WHERE id=?", (json.dumps(value), old["id"]))
            request = dict(id=uuid4().hex, owner=owner, state="pending", expires_at=time.time() + lifetime,
                           verification_kind="qr", identity=None, qr_url=None, error=None)
            db.execute("INSERT INTO bind_requests VALUES(?,?,?,?,?)", (request["id"], owner, request["state"], request["expires_at"], json.dumps(request)))
            return request

    def request(self, request_id, owner):
        with self.lock, self.db() as db:
            value = self.decode(db.execute("SELECT data FROM bind_requests WHERE id=? AND owner=?", (request_id, owner)).fetchone())
            if not value:
                raise WechatError("未找到連接請求。", 404)
            if value["expires_at"] <= time.time() and value["state"] not in {"confirmed", "cancelled", "failed", "conflict"}:
                value["state"] = "expired"
                db.execute("UPDATE bind_requests SET state='expired',data=? WHERE id=?", (json.dumps(value), request_id))
            return value

    def update_request(self, request_id, owner, **fields):
        with self.lock:
            value = self.request(request_id, owner)
            if value["state"] in {"confirmed", "cancelled", "expired", "conflict"}:
                return value
            allowed = {"state", "qr_url", "qr_data_url", "verification_kind", "identity", "account_id", "sender_id", "error", "expires_at", "choices"}
            value.update({k: v for k, v in fields.items() if k in allowed})
            with self.db() as db:
                db.execute("UPDATE bind_requests SET state=?,expires=?,data=? WHERE id=? AND owner=?",
                           (value["state"], value["expires_at"], json.dumps(value), request_id, owner))
            return value

    def confirm(self, request_id, owner, conversation_id):
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            request = self.decode(db.execute("SELECT data FROM bind_requests WHERE id=? AND owner=?", (request_id, owner)).fetchone())
            if not request:
                raise WechatError("未找到連接請求。", 404)
            if request["state"] == "confirmed":
                return self.decode(db.execute("SELECT data FROM bindings WHERE owner=?", (owner,)).fetchone())
            if request["expires_at"] <= time.time() or request["state"] != "verified":
                raise WechatError("請先完成有效的微信授權及身份確認。")
            account_id, sender_id = request.get("account_id"), request.get("sender_id")
            if not account_id or not sender_id or owner == "0000":
                raise WechatError("微信身份尚未驗證。", 403)
            old = self.decode(db.execute("SELECT data FROM bindings WHERE owner=?", (owner,)).fetchone())
            if old and old["state"] == "connected":
                raise WechatError("請先解除目前的微信連接。")
            # A gateway account or verified scanner cannot belong to two owners,
            # even when a new authorization returns a different counterpart ID.
            conflict = db.execute("SELECT owner FROM bindings WHERE (account_id=? OR sender_id=?) AND state='connected'", (account_id, sender_id)).fetchone()
            if conflict:
                raise WechatError("這個微信已連接其他小智帳號，請先解除原連接。")
            if db.execute("SELECT COUNT(*) FROM bindings WHERE state='connected'").fetchone()[0] >= 3:
                raise WechatError("本機試用最多連接三個獨立帳號。")
            value = dict(owner=owner, account_id=account_id, sender_id=sender_id, state="connected",
                         epoch=(old or {}).get("epoch", 0) + 1, conversation_id=conversation_id,
                         skill_id="none", identity=request.get("identity") or {"label": sender_id}, created_at=time.time())
            db.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?) ON CONFLICT(owner) DO UPDATE SET account_id=excluded.account_id,sender_id=excluded.sender_id,state=excluded.state,epoch=excluded.epoch,data=excluded.data",
                       (owner, account_id, sender_id, "connected", value["epoch"], json.dumps(value)))
            request.update(state="confirmed", qr_url=None, qr_data_url=None)
            db.execute("UPDATE bind_requests SET state='confirmed',data=? WHERE id=?", (json.dumps(request), request_id))
            return value

    def patch_binding(self, owner, **fields):
        with self.lock, self.db() as db:
            value = self.decode(db.execute("SELECT data FROM bindings WHERE owner=?", (owner,)).fetchone())
            if not value or value["state"] != "connected":
                raise WechatError("尚未連接微信。", 404)
            value.update({k: v for k, v in fields.items()
                          if k in {"conversation_id", "skill_id", "force_web", "force_think"}})
            db.execute("UPDATE bindings SET data=? WHERE owner=?", (json.dumps(value), owner))
            return value

    def disconnect(self, owner, reason=None):
        with self.lock, self.db() as db:
            value = self.decode(db.execute("SELECT data FROM bindings WHERE owner=?", (owner,)).fetchone())
            if value:
                value.update(state="disconnected", epoch=value["epoch"] + 1,
                             revoked_reason="auth_expired" if reason == "auth_expired" else None)
                db.execute("UPDATE bindings SET state='disconnected',epoch=?,data=? WHERE owner=?", (value["epoch"], json.dumps(value), owner))
            for row in db.execute("SELECT * FROM turns WHERE owner=? AND status NOT IN ('completed','aborted','interrupted')", (owner,)).fetchall():
                turn = self.decode(row)
                turn.update(status="aborted", error="微信連接已解除。")
                db.execute("UPDATE turns SET status='aborted',expires=0,data=? WHERE id=?", (json.dumps(turn), turn["id"]))
            for row in db.execute("SELECT * FROM outbox WHERE owner=? AND status!='sent'", (owner,)).fetchall():
                item = self.decode(row)
                item.update(status="cancelled", delivery_token=None)
                db.execute("UPDATE outbox SET status='cancelled',data=? WHERE id=?", (json.dumps(item), item["id"]))
            for row in db.execute("SELECT * FROM bind_requests WHERE owner=? AND state!='confirmed'", (owner,)).fetchall():
                request = self.decode(row)
                request["state"] = "cancelled"
                db.execute("UPDATE bind_requests SET state='cancelled',data=? WHERE id=?", (json.dumps(request), request["id"]))
            return value

    def create_turn(self, binding, event_id, text, file_ids=()):
        identity_key = json.dumps([binding["account_id"], binding["sender_id"], event_id], separators=(",", ":"))
        event_key = digest(identity_key)
        fingerprint = digest(json.dumps([text, list(file_ids)], sort_keys=True))
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self.decode(db.execute("SELECT data FROM turns WHERE event_key=?", (event_key,)).fetchone())
            if old:
                if old["fingerprint"] != fingerprint:
                    raise WechatError("重複訊息的內容不一致。")
                return old, None, False
            current = self.decode(db.execute("SELECT data FROM bindings WHERE owner=? AND state='connected'", (binding["owner"],)).fetchone())
            if not current or current["epoch"] != binding["epoch"]:
                raise WechatError("微信連接已失效。", 403)
            token = secrets.token_urlsafe(32)
            turn = dict(id=uuid4().hex, owner=binding["owner"], conversation_id=binding["conversation_id"],
                        account_id=binding["account_id"], sender_id=binding["sender_id"], epoch=binding["epoch"],
                        event_id=event_id, fingerprint=fingerprint, text=text, file_ids=list(file_ids), attachments=[],
                        status="queued", reply_id=uuid4().hex, skill_id=binding.get("skill_id", "none"), attempt=1,
                        created_at=time.time(), expires_at=time.time() + 900, prepared_context=None, error=None)
            db.execute("INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?)", (turn["id"], turn["owner"], turn["conversation_id"], event_key,
                       "queued", turn["epoch"], digest(token), turn["expires_at"], json.dumps(turn)))
            return turn, token, True

    def owned_turn(self, turn_id, owner):
        turn = self.turn(turn_id)
        if turn["owner"] != owner:
            raise WechatError("未找到本人任務。", 404)
        return turn

    def tasks(self, owner, conversation_id=None, statuses=None):
        with self.db() as db:
            rows = db.execute("SELECT data FROM turns WHERE owner=? ORDER BY rowid DESC LIMIT 100", (owner,)).fetchall()
        return [value for row in rows if (value := self.decode(row))
                and (conversation_id is None or value["conversation_id"] == conversation_id)
                and (statuses is None or value["status"] in statuses)]

    @staticmethod
    def event_key(binding, event_id):
        return digest(json.dumps([binding["account_id"], binding["sender_id"], event_id], separators=(",", ":")))

    def lookup_event(self, binding, event_id, text, file_ids):
        key = self.event_key(binding, event_id)
        fingerprint = digest(json.dumps([text, list(file_ids)], sort_keys=True))
        with self.db() as db:
            row = db.execute("SELECT * FROM wechat_events WHERE event_key=?", (key,)).fetchone()
        if not row:
            return None
        if row["fingerprint"] != fingerprint:
            raise WechatError("重複訊息的內容不一致。")
        return self.turn(row["turn_id"])

    def attach_waiting(self, binding, turn_id, event_id, text, fingerprint_ids, attachments):
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM turns WHERE id=? AND owner=?", (turn_id, binding["owner"])).fetchone()
            turn = self.decode(row)
            current = db.execute("SELECT epoch FROM bindings WHERE owner=? AND state='connected'", (binding["owner"],)).fetchone()
            if not turn or turn["status"] != "waiting_attachment" or not current or current["epoch"] != binding["epoch"]:
                raise WechatError("等待附件的任務已改變，請重試。")
            token = secrets.token_urlsafe(32)
            turn.update(status="queued", attachments=attachments, expires_at=time.time() + 900, updated_at=time.time(),
                        stage="附件已接收，等待處理", attachment_event_id=event_id)
            db.execute("INSERT INTO wechat_events VALUES(?,?,?)", (self.event_key(binding, event_id), turn_id,
                digest(json.dumps([text, list(fingerprint_ids)], sort_keys=True))))
            db.execute("UPDATE turns SET status='queued',cap_hash=?,expires=?,data=? WHERE id=?",
                       (digest(token), turn["expires_at"], json.dumps(turn), turn_id))
            return turn, token

    def suspend(self, turn_id, owner, reason="interrupted"):
        with self.lock:
            turn = self.owned_turn(turn_id, owner)
            if turn["status"] == "completed":
                return turn
            return self.update_turn(turn_id, status="interrupted", expires_at=0, updated_at=time.time(),
                stage="已暫停，可繼續", error="處理已暫停，已完成步驟和原件均保留。", suspend_reason=reason)

    def attach_instruction(self, binding, turn_id, event_id, text, fingerprint_ids):
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = self.decode(db.execute("SELECT data FROM turns WHERE id=? AND owner=?", (turn_id, binding["owner"])).fetchone())
            current = db.execute("SELECT epoch FROM bindings WHERE owner=? AND state='connected'", (binding["owner"],)).fetchone()
            if not turn or turn["status"] != "waiting_instruction" or turn["epoch"] != binding["epoch"] or not current or current["epoch"] != binding["epoch"]:
                raise WechatError("等待處理要求的任務已改變。")
            token = secrets.token_urlsafe(32)
            turn.update(status="queued", text=text, awaiting_instruction=False, expires_at=time.time()+900,
                        stage="處理要求已接收", updated_at=time.time(), instruction_event_id=event_id)
            db.execute("INSERT INTO wechat_events VALUES(?,?,?)", (self.event_key(binding,event_id),turn_id,digest(json.dumps([text,list(fingerprint_ids)],sort_keys=True))))
            db.execute("UPDATE turns SET status='queued',cap_hash=?,expires=?,data=? WHERE id=?",(digest(token),turn["expires_at"],json.dumps(turn),turn_id))
            return turn, token

    def resume(self, turn_id, binding):
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = self.decode(db.execute("SELECT data FROM turns WHERE id=? AND owner=?", (turn_id, binding["owner"])).fetchone())
            current = self.decode(db.execute("SELECT data FROM bindings WHERE owner=? AND state='connected'", (binding["owner"],)).fetchone())
            if not turn or not current or current["epoch"] != binding["epoch"]:
                raise WechatError("未找到可繼續的本人任務。", 404)
            if turn["epoch"] != binding["epoch"] or turn["account_id"] != binding["account_id"] or turn["sender_id"] != binding["sender_id"]:
                raise WechatError("原任務的微信授權已撤銷，不能恢復舊投遞。", 403)
            if turn["conversation_id"] != binding["conversation_id"]:
                raise WechatError("請先在微信連接設定選擇這個任務的原會話。")
            if turn["status"] not in {"interrupted", "aborted"} or turn.get("result"):
                raise WechatError("只有未完成且已暫停的任務可以繼續。")
            token = secrets.token_urlsafe(32)
            turn.update(status="queued", epoch=binding["epoch"], account_id=binding["account_id"], sender_id=binding["sender_id"],
                        attempt=turn.get("attempt", 1) + 1, expires_at=time.time() + 900, error=None,
                        updated_at=time.time(), stage="等待繼續處理", resume_requested_at=time.time())
            if turn.get("awaiting_attachment") and not turn.get("attachments"):
                turn.update(status="waiting_attachment", stage="等待本次附件")
            if turn.get("awaiting_instruction"):
                turn.update(status="waiting_instruction", stage="等待處理要求")
            db.execute("UPDATE turns SET status=?,epoch=?,cap_hash=?,expires=?,data=? WHERE id=?",
                       (turn["status"], turn["epoch"], digest(token), turn["expires_at"], json.dumps(turn), turn_id))
            return turn, token

    def begin_tool(self, turn_id, call_id, name, arguments):
        fingerprint = digest(json.dumps([name, arguments], sort_keys=True, ensure_ascii=False))
        with self.lock, self.db() as db:
            row = db.execute("SELECT * FROM wechat_tool_calls WHERE turn_id=? AND call_id=?", (turn_id, call_id)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise WechatError("同一工具呼叫編號的內容不一致。")
                value = self.decode(row)
                if row["state"] == "completed":
                    return value
            value = {"call_id": call_id, "name": name, "state": "running", "started_at": time.time()}
            db.execute("INSERT INTO wechat_tool_calls VALUES(?,?,?,?,?) ON CONFLICT(turn_id,call_id) DO UPDATE SET state=excluded.state,data=excluded.data",
                       (turn_id, call_id, fingerprint, "running", json.dumps(value)))
            return value

    def finish_tool(self, turn_id, call_id, result):
        with self.lock, self.db() as db:
            row = db.execute("SELECT * FROM wechat_tool_calls WHERE turn_id=? AND call_id=?", (turn_id, call_id)).fetchone()
            if not row:
                raise WechatError("工具執行記錄不存在。", 404)
            value = self.decode(row)
            value.update(state="completed", completed_at=time.time(), result=result)
            db.execute("UPDATE wechat_tool_calls SET state='completed',data=? WHERE turn_id=? AND call_id=?",
                       (json.dumps(value), turn_id, call_id))
            return value

    def tool_progress(self, turn_id):
        with self.db() as db:
            rows = db.execute("SELECT data FROM wechat_tool_calls WHERE turn_id=? ORDER BY rowid", (turn_id,)).fetchall()
        return [self.decode(row) for row in rows]

    def control(self, binding, event_id, result=None):
        key = self.event_key(binding, "control:" + event_id)
        with self.lock, self.db() as db:
            if result is not None:
                db.execute("INSERT OR IGNORE INTO wechat_controls VALUES(?,?)", (key, json.dumps(result)))
            row = db.execute("SELECT data FROM wechat_controls WHERE event_key=?", (key,)).fetchone()
            return self.decode(row)

    def turn(self, turn_id, capability=None, *, allow_completed=False):
        with self.db() as db:
            row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if not row:
                raise WechatError("未找到微信回合。", 404)
            value = self.decode(row)
            if capability is not None:
                if not capability or not hmac.compare_digest(row["cap_hash"], digest(capability)):
                    raise WechatError("回合授權無效。", 403)
                binding = self.decode(db.execute("SELECT data FROM bindings WHERE owner=? AND state='connected'", (row["owner"],)).fetchone())
                if not binding or binding["epoch"] != row["epoch"]:
                    raise WechatError("微信連接已撤銷。", 403)
                # An already-completed immutable result can be acknowledged
                # after recovery without renewing execution authority. Identity,
                # epoch and capability were still checked above.
                completed_replay = allow_completed and row["status"] == "completed"
                if (row["expires"] <= time.time() and not completed_replay) or (row["status"] in {"aborted", "interrupted"}):
                    raise WechatError("回合已失效，請重新發送訊息。", 409)
                if row["status"] == "completed" and not allow_completed:
                    raise WechatError("這個回合已完成。")
            return value

    def update_turn(self, turn_id, **fields):
        with self.lock, self.db() as db:
            row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if not row:
                raise WechatError("未找到微信回合。", 404)
            value = self.decode(row)
            value.update(fields)
            db.execute("UPDATE turns SET status=?,expires=?,data=? WHERE id=?",
                       (value["status"], value["expires_at"], json.dumps(value), turn_id))
            return value

    def begin_commit(self, turn_id, advancement_key, text, artifacts):
        fingerprint = digest(json.dumps([advancement_key, text, artifacts], sort_keys=True, ensure_ascii=False))
        with self.lock:
            turn = self.turn(turn_id)
            if turn.get("commit_fingerprint") and turn["commit_fingerprint"] != fingerprint:
                raise WechatError("同一回合不能提交不同的回答。")
            return self.update_turn(turn_id, status="committing", commit_fingerprint=fingerprint,
                commit_payload={"advancement_key": advancement_key, "text": text, "artifacts": artifacts})

    def interrupted_commits(self):
        with self.db() as db:
            return [value for row in db.execute("SELECT * FROM turns WHERE status='interrupted'").fetchall()
                    if (value := self.decode(row)).get("commit_payload")]

    def record_stage(self, turn_id, capability, stage_key, message, *, completed_steps=None, metadata=None):
        """Record an observed execution stage under the current turn lease."""
        if not isinstance(stage_key, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", stage_key):
            raise WechatError("進度階段無效。", 422)
        if not isinstance(message, str) or not message.strip() or len(message) > 300:
            raise WechatError("進度說明無效。", 422)
        if completed_steps is not None and (not isinstance(completed_steps, (list, tuple)) or len(completed_steps) > 20 or any(
                not isinstance(step, str) or len(step) > 100 for step in completed_steps)):
            raise WechatError("已完成步驟無效。", 422)
        if metadata is not None and (not isinstance(metadata, dict) or len(json.dumps(metadata, ensure_ascii=False)) > 4000):
            raise WechatError("進度附加資料無效。", 422)
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = self.turn(turn_id, capability)
            if turn["status"] not in {"queued", "running"}:
                raise WechatError("回合目前不能更新進度。", 409)
            steps = (turn.get("completed_steps") or []) if completed_steps is None else list(dict.fromkeys(step.strip() for step in completed_steps if step.strip()))
            changed = (turn.get("stage_key"), turn.get("stage"), turn.get("completed_steps")) != (stage_key, message.strip(), steps)
            turn.update(stage_key=stage_key, stage=message.strip(), completed_steps=steps,
                        stage_metadata=metadata or {}, stage_updated_at=time.time(),
                        stage_revision=int(turn.get("stage_revision", 0)) + int(changed))
            db.execute("UPDATE turns SET data=? WHERE id=?", (json.dumps(turn, ensure_ascii=False), turn_id))
            return turn

    def enqueue_progress(self, turn_id, capability, elapsed):
        """Send current observed work; repeated timer ticks cannot invent progress."""
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = self.turn(turn_id, capability)
            if turn["status"] not in {"queued", "running"}:
                return False
            seconds = max(0, int(elapsed))
            slot = 0 if seconds < 30 else seconds // 30
            revision = int(turn.get("stage_revision", 0))
            prefix = f"progress:{turn_id}:{turn.get('attempt', 1)}:"
            count = db.execute("SELECT COUNT(*) FROM outbox WHERE turn_id LIKE ?", (prefix + "%",)).fetchone()[0]
            if count >= 10:
                return False
            key = f"{prefix}{revision}:{slot}"
            stage = turn.get("stage") or ("正在排隊等待處理" if turn["status"] == "queued" else "處理已開始，等待下一項進度")
            completed = turn.get("completed_steps") or []
            finished_tools = db.execute("SELECT COUNT(*) FROM wechat_tool_calls WHERE turn_id=? AND state='completed'", (turn_id,)).fetchone()[0]
            details = "已完成：" + "、".join(completed[-3:]) + "。" if completed else ""
            if finished_tools:
                details += f"已完成 {finished_tools} 項工具步驟。"
            item = dict(id=uuid4().hex, turn_id=turn_id, progress=True, owner=turn["owner"], epoch=turn["epoch"],
                        attempt=turn.get("attempt", 1), stage_revision=revision, stage_key=turn.get("stage_key"),
                        target={"account_id": turn["account_id"], "sender_id": turn["sender_id"]},
                        text=f"{stage}；已耗時 {seconds} 秒。" + details, artifacts=[],
                        status="pending", attempts=0, delivery_token=None, available_at=time.time())
            cursor = db.execute("INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?)", (item["id"], key, turn["owner"], "pending", json.dumps(item)))
            return bool(cursor.rowcount)

    def enqueue_note(self, turn_id, capability, text, sequence):
        """Queue one model-written progress line; the same sequence is sent once."""
        if not isinstance(text, str) or not text.strip() or len(text) > 200:
            return False
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = self.turn(turn_id, capability)
            if turn["status"] not in {"queued", "running"}:
                return False
            prefix = f"progress:{turn_id}:{turn.get('attempt', 1)}:"
            if db.execute("SELECT COUNT(*) FROM outbox WHERE turn_id LIKE ?", (prefix + "%",)).fetchone()[0] >= 10:
                return False
            item = dict(id=uuid4().hex, turn_id=turn_id, progress=True, owner=turn["owner"], epoch=turn["epoch"],
                        attempt=turn.get("attempt", 1),
                        target={"account_id": turn["account_id"], "sender_id": turn["sender_id"]},
                        text=text.strip(), artifacts=[], status="pending", attempts=0,
                        delivery_token=None, available_at=time.time())
            cursor = db.execute("INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?)",
                                (item["id"], f"{prefix}note:{int(sequence)}", turn["owner"], "pending", json.dumps(item)))
            return bool(cursor.rowcount)

    def cancel_progress(self, turn_id):
        """A finished turn never sends progress that was still waiting."""
        with self.lock, self.db() as db:
            for row in db.execute("SELECT * FROM outbox WHERE turn_id LIKE ? AND status IN ('pending','failed')",
                                  (f"progress:{turn_id}:%",)).fetchall():
                item = self.decode(row)
                item.update(status="cancelled", delivery_token=None)
                db.execute("UPDATE outbox SET status='cancelled',data=? WHERE id=?", (json.dumps(item), item["id"]))

    def notify(self, turn_id, text):
        """Tell the user once that this turn did not produce an answer."""
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = self.decode(db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone())
            if not turn or turn["status"] == "completed":
                return False
            binding = self.decode(db.execute("SELECT data FROM bindings WHERE owner=? AND state='connected'", (turn["owner"],)).fetchone())
            if not binding or binding["epoch"] != turn["epoch"]:
                return False
            item = dict(id=uuid4().hex, turn_id=turn_id, notice=True, owner=turn["owner"], epoch=turn["epoch"],
                        target={"account_id": turn["account_id"], "sender_id": turn["sender_id"]},
                        text=text, artifacts=[], status="pending", attempts=0, delivery_token=None, available_at=time.time())
            cursor = db.execute("INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?)",
                                (item["id"], f"notice:{turn_id}:{turn.get('attempt', 1)}", turn["owner"], "pending", json.dumps(item)))
            return bool(cursor.rowcount)

    @staticmethod
    def _progress_active(db, item):
        if not item.get("progress"):
            return True
        row = db.execute("SELECT data FROM turns WHERE id=?", (item["turn_id"],)).fetchone()
        turn = json.loads(row["data"]) if row else None
        return bool(turn and turn["status"] in {"queued", "running"} and
                    turn.get("attempt", 1) == item.get("attempt", 1) and
                    ("stage_revision" not in item or item["stage_revision"] == turn.get("stage_revision", 0)) and
                    turn.get("expires_at", 0) > time.time())

    def finish(self, turn_id, advancement_key, text, artifacts, result, request_fingerprint=None, reconcile=False):
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            turn = self.decode(row)
            commit_fingerprint = request_fingerprint or digest(json.dumps([advancement_key, text, artifacts], sort_keys=True, ensure_ascii=False))
            if turn.get("commit_fingerprint") and turn["commit_fingerprint"] != commit_fingerprint:
                raise WechatError("同一回合不能提交不同的回答。")
            if turn["status"] == "completed":
                return turn
            binding = self.decode(db.execute("SELECT data FROM bindings WHERE owner=? AND state='connected'", (turn["owner"],)).fetchone())
            if not binding or binding["epoch"] != turn["epoch"] or turn["status"] == "aborted" or (turn["status"] == "interrupted" and not reconcile):
                raise WechatError("微信連接已撤銷。", 403)
            turn.update(status="completed", advancement_key=advancement_key, commit_fingerprint=commit_fingerprint,
                        result=result, completed_at=time.time())
            db.execute("UPDATE turns SET status='completed',data=? WHERE id=?", (json.dumps(turn), turn_id))
            item = dict(id=uuid4().hex, turn_id=turn_id, owner=turn["owner"], epoch=turn["epoch"],
                        target={"account_id": turn["account_id"], "sender_id": turn["sender_id"]}, text=text,
                        artifacts=artifacts, status="pending", attempts=0, delivery_token=None, available_at=time.time())
            db.execute("INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?)", (item["id"], turn_id, turn["owner"], "pending", json.dumps(item)))
            return turn

    def claim_outbox(self, limit=10):
        now = time.time()
        with self.db() as db:
            candidates = db.execute(
                "SELECT data FROM outbox WHERE status IN ('pending','failed','sending') ORDER BY rowid LIMIT 100"
            ).fetchall()
        if not any(self.decode(row).get("available_at", 0) <= now for row in candidates):
            return []
        result = []
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM outbox WHERE status IN ('pending','failed','sending') ORDER BY rowid LIMIT 100").fetchall():
                item = self.decode(row)
                if item["status"] == "sending":
                    if item.get("available_at", 0) <= time.time():
                        item.update(status="uncertain", delivery_token=None, error="發送狀態未確認，請核對微信後再重試。")
                        db.execute("UPDATE outbox SET status='uncertain',data=? WHERE id=?", (json.dumps(item), item["id"]))
                    continue
                if item.get("available_at", 0) > time.time():
                    continue
                binding = db.execute("SELECT epoch FROM bindings WHERE owner=? AND state='connected'", (item["owner"],)).fetchone()
                if not binding or binding["epoch"] != item["epoch"] or not self._progress_active(db, item):
                    item.update(status="cancelled", delivery_token=None)
                    db.execute("UPDATE outbox SET status='cancelled',data=? WHERE id=?", (json.dumps(item), item["id"]))
                    continue
                item.update(status="sending", attempts=item["attempts"] + 1, delivery_token=secrets.token_urlsafe(24), available_at=time.time() + 60)
                db.execute("UPDATE outbox SET status='sending',data=? WHERE id=?", (json.dumps(item), item["id"]))
                result.append(item)
                if len(result) >= limit:
                    break
        return result

    def ack(self, item_id, token, status, error=None):
        with self.lock, self.db() as db:
            item = self.decode(db.execute("SELECT data FROM outbox WHERE id=?", (item_id,)).fetchone())
            if not item:
                raise WechatError("未找到投遞記錄。", 404)
            if not isinstance(token, str) or not token or not item.get("delivery_token") or not hmac.compare_digest(item["delivery_token"], token):
                raise WechatError("投遞授權已失效。", 403)
            binding = db.execute("SELECT epoch FROM bindings WHERE owner=? AND state='connected'", (item["owner"],)).fetchone()
            if not binding or binding["epoch"] != item["epoch"]:
                raise WechatError("微信連接已撤銷。", 403)
            if item["status"] == "sent":
                return item
            item.update(status=status, error=("附件或訊息發送失敗，可重試。" if error else None),
                        available_at=time.time() + min(300, 2 ** min(item["attempts"], 8)))
            db.execute("UPDATE outbox SET status=?,data=? WHERE id=?", (status, json.dumps(item), item_id))
            return item

    def authorize_delivery(self, item_id, token):
        """Recheck a claimed recipient immediately before any network send."""
        with self.db() as db:
            item = self.decode(db.execute("SELECT data FROM outbox WHERE id=?", (item_id,)).fetchone())
            if (not item or not isinstance(token, str) or not token or not item.get("delivery_token")
                    or not hmac.compare_digest(item["delivery_token"], token)
                    or item["status"] != "sending" or item.get("available_at", 0) <= time.time()):
                raise WechatError("投遞授權已失效。", 403)
            binding = db.execute("SELECT epoch FROM bindings WHERE owner=? AND state='connected'", (item["owner"],)).fetchone()
            if not binding or binding["epoch"] != item["epoch"] or not self._progress_active(db, item):
                raise WechatError("微信連接已撤銷。", 403)
            return item

    def recover(self):
        with self.lock, self.db() as db:
            for row in db.execute("SELECT * FROM turns WHERE status IN ('queued','running','assembling','committing')").fetchall():
                value = self.decode(row)
                value.update(status="interrupted", expires_at=0, error="服務重啟，本回合已中斷；請重新發送訊息。")
                db.execute("UPDATE turns SET status='interrupted',expires=0,data=? WHERE id=?", (json.dumps(value), value["id"]))
            # In-flight delivery is uncertain: do not automatically resend and claim exactly-once delivery.
            for row in db.execute("SELECT * FROM outbox WHERE status='sending'").fetchall():
                value = self.decode(row)
                value.update(status="uncertain", delivery_token=None, error="發送狀態未確認，請核對微信後再重試。")
                db.execute("UPDATE outbox SET status='uncertain',data=? WHERE id=?", (json.dumps(value), value["id"]))
