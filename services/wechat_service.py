"""Authenticated local OpenClaw bridge and account-owned WeChat settings.

The bridge generates replies with its native Huawei MaaS provider. This module
only authorizes business tools, canonical context, final commits and delivery.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import io
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import Body, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse

from config.wechat_model import WECHAT_PROVIDER, WECHAT_MODEL, WECHAT_MODEL_NAME
from utils.image_utils import photo_name
from services.wechat_store import WechatError, WechatStore, digest


def local_settings():
    """Read only this profile's bridge settings, never export or log secrets."""
    from config.settings import ENABLE_WECHAT, DATA_DIR
    if not ENABLE_WECHAT:
        return {}
    values = {}
    path = Path(os.environ.get("HOUKONG_WECHAT_ENV_FILE") or Path(DATA_DIR) / "private" / "openclaw" / ".env")
    try:
        if path.is_file():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() in {"HOUKONG_WECHAT_SERVICE_TOKEN", "HOUKONG_WECHAT_BROKER_URL"}:
                    values[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    for key in ("HOUKONG_WECHAT_SERVICE_TOKEN", "HOUKONG_WECHAT_BROKER_URL"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def _local_port_open(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=0.8):
            return True
    except OSError:
        return False


def _gateway_profile_dir() -> Path:
    env_file = os.environ.get("HOUKONG_WECHAT_ENV_FILE")
    if env_file:
        return Path(env_file).resolve().parent
    from config.settings import DATA_DIR
    return Path(DATA_DIR) / "private" / "openclaw"


class LocalBridge:
    def __init__(self, token, url="http://127.0.0.1:18790"):
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
            raise ValueError("WeChat broker must use a local loopback HTTP address")
        self.token, self.url = token, url.rstrip("/")

    async def call(self, method, path, data=None):
        if not self.token:
            raise WechatError("本機微信服務尚未配置。", 503)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=3), trust_env=False) as client:
                response = await client.request(method, self.url + path,
                    headers={"X-Houkong-Wechat-Token": self.token}, json=data)
                response.raise_for_status()
                result = response.json()
                return result.get("data", result) if isinstance(result, dict) else {}
        except (httpx.HTTPError, ValueError):
            raise WechatError("本機 OpenClaw 微信服務未就緒，請先啟動本機服務。", 503) from None


def public_request(value):
    allowed = {"id", "state", "qr_url", "qr_data_url", "expires_at", "verification_kind", "identity", "error", "choices"}
    return {key: item for key, item in value.items() if key in allowed}


def public_turn(turn, capability=None, created=False):
    value = {"id": turn["id"], "state": turn["status"], "conversation_id": turn["conversation_id"],
             "reply_id": turn["reply_id"], "created": created, "duplicate": not created,
             "attachments": turn.get("attachments", []), "attempt": turn.get("attempt", 1),
             "question": turn.get("text", ""), "resume_summary": turn.get("resume_summary")}
    if capability:
        value["capability"] = capability
    if turn.get("result"):
        value["result"] = turn["result"]
    return value


def canonical_reply_text(text, prepared_context):
    """The durable reply and outbox share the same verified file notice."""
    text = text.strip()
    delivery = (prepared_context or {}).get("original_delivery")
    notice = delivery.get("text") if delivery else None
    if notice and notice not in text:
        text += "\n\n" + notice
    return text


class WechatController:
    def __init__(self, host, root=None, bridge=None, token=None):
        self.host = host
        self.store = WechatStore(root or host.DATA_DIR)
        self.store.recover()
        values = local_settings()
        self.token = token if token is not None else getattr(host, "wechat_service_token", values.get("HOUKONG_WECHAT_SERVICE_TOKEN", ""))
        self.bridge = bridge or getattr(host, "wechat_bridge", None) or LocalBridge(self.token, values.get("HOUKONG_WECHAT_BROKER_URL", "http://127.0.0.1:18790"))
        self.turn_locks = {}
        self.active = {}
        self.binding_locks = {}
        self.expiry_worker = None
        self.recovered = False
        self.recovery_lock = asyncio.Lock()
        self.running_tools = {}
        self.settlers = set()
        # Capabilities stay in this backend process only. Hook instances/workers
        # exchange a run ID, never re-select authority from their current session.
        self.native_run_caps = {}
        self.native_run_locks = {}
        self.progress_workers = {}
        self.web_tasks = {}
        # 預設：微信只做轉發，回合交給網站同一條流水線。設 HOUKONG_WECHAT_NATIVE_GENERATION=1
        # 可臨時退回由 OpenClaw 原生生成的舊流程（用於排查）。
        self.forward_to_website = os.environ.get("HOUKONG_WECHAT_NATIVE_GENERATION", "") != "1"
        self.auto_manage_gateway = False
        self.gateway_lock = asyncio.Lock()
        self.gateway_supervisor_task = None
        self.last_gateway_launch = 0.0

    def refresh_local_token(self):
        if not self.token and isinstance(self.bridge, LocalBridge):
            values = local_settings()
            token = getattr(self.host, "wechat_service_token", values.get("HOUKONG_WECHAT_SERVICE_TOKEN", ""))
            if token:
                self.token = token
                self.bridge.token = token

    @staticmethod
    def _spawn_gateway_process(profile_dir: Path, launcher: Path) -> None:
        import subprocess
        log_dir = profile_dir / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            stdout_file = open(log_dir / "local-start.stdout.log", "ab")
            stderr_file = open(log_dir / "local-start.stderr.log", "ab")
        except OSError:
            stdout_file = subprocess.DEVNULL
            stderr_file = subprocess.DEVNULL
        env = dict(os.environ)
        for proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(proxy_key, None)
        env["NO_PROXY"] = "localhost,127.0.0.1,::1,.qq.com,.weixin.qq.com,.myhuaweicloud.com"
        env["no_proxy"] = env["NO_PROXY"]
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            cmd = [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", str(launcher)]
        else:
            cmd = [str(launcher)]
        try:
            subprocess.Popen(
                cmd,
                cwd=str(profile_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                creationflags=creationflags,
                close_fds=True,
            )
        except OSError:
            pass
        finally:
            for stream in (stdout_file, stderr_file):
                if hasattr(stream, "close"):
                    try:
                        stream.close()
                    except OSError:
                        pass

    async def ensure_gateway_running(self, wait_seconds: float = 12.0) -> bool:
        if not self.auto_manage_gateway or not isinstance(self.bridge, LocalBridge):
            return False
        self.refresh_local_token()
        if not self.token:
            return False
        try:
            status = await self.bridge.call("GET", "/status")
            if status.get("available", status.get("ready", status.get("running", False))):
                return True
        except WechatError:
            pass
        async with self.gateway_lock:
            try:
                status = await self.bridge.call("GET", "/status")
                if status.get("available", status.get("ready", status.get("running", False))):
                    return True
            except WechatError:
                pass
            profile_dir = _gateway_profile_dir()
            launcher = profile_dir / ("gateway.cmd" if os.name == "nt" else "gateway.sh")
            try:
                exists = launcher.is_file()
            except OSError:
                exists = False
            if not exists:
                return False
            now = time.monotonic()
            if not _local_port_open(18789) and (now - self.last_gateway_launch >= 10.0):
                self.last_gateway_launch = now
                await asyncio.to_thread(self._spawn_gateway_process, profile_dir, launcher)
            deadline = time.monotonic() + max(0.5, wait_seconds)
            while time.monotonic() < deadline:
                await asyncio.sleep(0.8)
                try:
                    status = await self.bridge.call("GET", "/status")
                    if status.get("available", status.get("ready", status.get("running", False))):
                        return True
                except WechatError:
                    continue
            return False

    def start_gateway_supervisor(self):
        if not isinstance(self.bridge, LocalBridge):
            return
        self.auto_manage_gateway = True
        if self.gateway_supervisor_task is None or self.gateway_supervisor_task.done():
            self.gateway_supervisor_task = asyncio.create_task(self._supervise_gateway())

    async def _supervise_gateway(self):
        try:
            while self.auto_manage_gateway:
                try:
                    await self.ensure_gateway_running(wait_seconds=20.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(15.0)
        except asyncio.CancelledError:
            pass

    async def report_progress(self, turn_id, capability):
        started = time.monotonic()
        try:
            await asyncio.sleep(8)
            # The outbox deduplicates unchanged stages. Check frequently enough
            # that a real retrieval -> generation transition is visible.
            for _ in range(225):
                await self.db("enqueue_progress", turn_id, capability, time.monotonic() - started)
                await asyncio.sleep(2)
        except (asyncio.CancelledError, HTTPException):
            pass

    # ---- 微信純轉發：回合交給網站同一個聊天處理函數 ----

    FAILURE_TEXT = "這條訊息沒有處理成功，請再發一次。"

    def uses_web_pipeline(self, turn):
        """文字與附件訊息都走網站流水線；只有恢復中的舊原生任務留在原流程。"""
        if any(photo_name(item.get("name") or "") for item in turn.get("attachments", ())):
            context = turn.get("prepared_context") or {}
            return not context or context.get("pipeline") == "web"
        return self.forward_to_website and turn.get("attempt", 1) == 1

    def nudge_outbox(self):
        """請本機通道立即投遞，不必等下一次輪詢；失敗時輪詢仍會送出。"""
        async def push():
            try:
                await self.broker("POST", "/outbox/flush", {})
            except Exception:
                pass
        task = asyncio.create_task(push())
        self.settlers.add(task)
        task.add_done_callback(self.settlers.discard)

    async def web_progress(self, turn_id, capability, state):
        """按模型寫的實際進度播報：每 5 秒看一次有沒有新進度；
        一般處理滿 8 秒、文件處理滿 6 秒仍未播報過，就強制說一聲。"""
        started, last_tick = time.monotonic(), 0.0
        try:
            while True:
                await asyncio.sleep(0.5)
                elapsed = time.monotonic() - started
                note, text = state["note"], None
                if not state["sent_any"] and elapsed >= (6 if state["kind"] == "file" else 8):
                    text = (note + "，" if note else "") + "正在盡快處理中"
                elif elapsed - last_tick >= 5:
                    last_tick = elapsed
                    if note and state["worth"] and note != state["sent"]:
                        text = note
                if text:
                    state["sequence"] += 1
                    if await self.db("enqueue_note", turn_id, capability, text, state["sequence"]):
                        state.update(sent=note, sent_any=True)
                        self.nudge_outbox()
        except (asyncio.CancelledError, HTTPException):
            pass

    async def run_web(self, turn, capability):
        turn_id, owner, cid = turn["id"], turn["owner"], turn["conversation_id"]
        state = {"note": None, "worth": False, "kind": "chat", "sent": None, "sent_any": False, "sequence": 0}
        self.progress_workers[turn_id] = asyncio.create_task(self.web_progress(turn_id, capability, state))
        started = time.monotonic()

        async def on_event(event):
            kind = event.get("type")
            if kind == "status" and event.get("status") == "planned":
                if event.get("progress_note"):
                    state.update(note=event["progress_note"], worth=bool(event.get("worth_telling")))
            elif kind == "file_status":
                state["kind"] = "file"
                if event.get("progress_note"):
                    state.update(note=event["progress_note"], worth=True)
                await self.db("update_turn", turn_id, expires_at=time.time() + 900)

        notice = None
        try:
            await self.chat.acquire_turn(owner, cid, turn_id)
            self.active[turn_id] = (owner, cid)
            if self.expiry_worker is None or self.expiry_worker.done():
                self.expiry_worker = asyncio.create_task(self.expire_active())
            binding = await self.db("binding", owner) or {}
            file_ids = list(turn.get("file_ids", []))
            if turn.get("attachments"):
                # 微信附件＝在網站上傳：存入本人資料庫後，以一般檔案引用進入同一條流水線。
                state["kind"] = "file"
                imported = await self.chat.import_attachments(owner, cid, turn["attachments"])
                file_ids = list(dict.fromkeys([*file_ids, *imported]))
                await self.db("update_turn", turn_id, file_ids=file_ids, expires_at=time.time() + 900)
            photos_only = len(file_ids) == len(turn.get("attachments", ())) and all(
                photo_name(item.get("name") or "") for item in turn.get("attachments", ())) and bool(file_ids)
            question = turn["text"] or ("" if photos_only else "請查看這則訊息的附件。")
            result = await self.chat.run_web_turn(
                owner, cid, question, skill_id=turn.get("skill_id", "none"), file_ids=file_ids,
                force_web=bool(binding.get("force_web")), force_think=bool(binding.get("force_think")), on_event=on_event)
            text = result["text"]
            if result["error"] or not text:
                notice = result["error"] or self.FAILURE_TEXT
                raise RuntimeError("web turn produced no complete answer")
            if turn.get("attachments") and "📎" not in text:
                # 告訴用戶附件自動存到了哪個分類（公開範圍需另行確認）。
                try:
                    from services import file_sharing
                    saved = [record for record in await file_sharing.owned_files(self.host, owner, file_ids)
                             if record["id"] not in turn.get("file_ids", [])]
                    if saved:
                        text += "\n\n📎 已存入你的資料庫：" + "；".join(
                            "《" + record.get("name", "文件") + "》（" + file_sharing.classification_text(self.host, record) + "）"
                            for record in saved) + "。如需公開給班級、年級或全校，直接告訴我。"
                except Exception:
                    pass
            preference = result.get("preference")
            if preference:
                changes, said = {}, []
                for key, wish, label in (("force_web", preference.get("web_wish"), "聯網"),
                                         ("force_think", preference.get("think_wish"), "深度思考")):
                    if wish in {"force", "forbid"}:
                        changes[key] = wish == "force"
                        said.append(("以後都會為你" if wish == "force" else "以後不再固定") + label)
                if changes:
                    async with self.binding_locks.setdefault(owner, asyncio.Lock()):
                        await self.db("patch_binding", owner, **changes)
                    text += "\n\n（已記住：" + "、".join(said) + "。可在網站「微信連接」隨時更改。）"
            artifacts = []
            for item in result["artifacts"]:
                record = await self.host.data_store.find_file_by_id(str(item.get("id", "")))
                if record and self.host.check_file_permission(record, owner):
                    artifacts.append({"id": record["id"], "name": record.get("name") or item.get("name") or "文件"})
            advancement = f"web-turn:{turn_id}:{turn.get('attempt', 1)}"
            payload = {"reply_id": result["message_id"], "message_id": result["message_id"], "conversation_id": cid,
                       "text": text, "artifacts": artifacts, "provider": WECHAT_PROVIDER, "model": result.get("model") or WECHAT_MODEL,
                       "web": result["web"], "think": result["think"],
                       "seconds": round(time.monotonic() - started, 2)}
            async with self.binding_locks.setdefault(owner, asyncio.Lock()):
                await self.db("begin_commit", turn_id, advancement, text, artifacts)
                await self.db("finish", turn_id, advancement, text, artifacts, payload)
            await self.db("cancel_progress", turn_id)
            self.nudge_outbox()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if isinstance(error, HTTPException) and isinstance(error.detail, str) and error.status_code in {400, 409, 429}:
                notice = error.detail
            try:
                current = await self.db("turn", turn_id)
                if current["status"] != "completed":
                    await self.db("cancel_progress", turn_id)
                    if current.get("suspend_reason") != "user_pause":
                        await self.db("notify", turn_id, notice or self.FAILURE_TEXT)
                    await self.db("update_turn", turn_id, status="interrupted", expires_at=0,
                                  stage="未完成", error=notice or self.FAILURE_TEXT)
                    self.nudge_outbox()
            except Exception:
                pass
        finally:
            self.web_tasks.pop(turn_id, None)
            await self.release(turn_id)

    async def record_stage(self, turn_id, capability, stage, message, **metadata):
        completed = metadata.pop("completed_steps", None)
        return await self.db("record_stage", turn_id, capability, stage, message,
                             completed_steps=completed, metadata=metadata)

    async def db(self, method, *args, **kwargs):
        try:
            return await asyncio.to_thread(getattr(self.store, method), *args, **kwargs)
        except WechatError as error:
            raise HTTPException(error.status, str(error)) from None

    @property
    def chat(self):
        chat = getattr(self.host, "wechat_chat", None)
        if chat is None:
            raise HTTPException(503, "微信業務接入尚未就緒。")
        return chat

    async def broker(self, method, path, data=None):
        try:
            return await self.bridge.call(method, path, data)
        except WechatError as error:
            raise HTTPException(error.status, str(error)) from None

    async def owner(self, session_id):
        session = await self.host.data_store.get_session(session_id)
        if not session or not session.get("account"):
            raise HTTPException(401, "請先登入。")
        return session["account"]

    def internal(self, token):
        if not self.token or not token or not hmac.compare_digest(self.token, token):
            raise HTTPException(401, "本機服務認證失敗。")

    @staticmethod
    def _run_id(run_id):
        if not isinstance(run_id, str) or not 0 < len(run_id) <= 512:
            raise HTTPException(422, "原生執行編號無效。")
        return run_id

    @staticmethod
    def _run_state(run):
        return {"state": run["state"], "turn_id": run["turn_id"]}

    async def native_run_start(self, turn_id, capability, body):
        if set(body) != {"run_id"}:
            raise HTTPException(422, "原生執行授權欄位無效。")
        run_id = self._run_id(body["run_id"])
        lock = self.native_run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            turn = await self.db("turn", turn_id, capability, allow_completed=True)
            previous = None
            try:
                previous = await self.db("native_run", run_id)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
            if turn["status"] != "running" or self.active.get(turn_id) != (turn["owner"], turn["conversation_id"]):
                if previous and previous["state"] == "completed" and previous["turn_id"] == turn_id and previous["cap_hash"] == digest(capability):
                    return self._run_state(previous)
                raise HTTPException(409, "本回合沒有有效的原生執行權。")
            run = await self.db("native_run_start", run_id, turn_id, turn.get("attempt", 1), turn["epoch"], digest(capability))
            # A pause/rebind may have happened during the durable write.
            await self.db("turn", turn_id, capability)
            self.native_run_caps[run_id] = capability
            await self.record_stage(turn_id, capability, "generating", "正在請求盤古生成回覆")
            return self._run_state(run)

    async def _native_authority(self, run_id):
        run = await self.db("native_run", run_id)
        capability = self.native_run_caps.get(run_id)
        if capability is None:
            raise HTTPException(409, "原生執行授權已失效，請繼續原任務以建立新的執行。")
        turn = await self.db("turn", run["turn_id"], capability, allow_completed=True)
        if run["cap_hash"] != digest(capability) or run["attempt"] != turn.get("attempt", 1) or run["epoch"] != turn["epoch"]:
            raise HTTPException(409, "原生執行屬於舊的回合批次。")
        if run["state"] != "completed" and (turn["status"] != "running" or self.active.get(turn["id"]) != (turn["owner"], turn["conversation_id"])):
            raise HTTPException(409, "原生執行已沒有目前回合的執行權。")
        return run, turn, capability

    async def native_output(self, run_id, body):
        self._run_id(run_id)
        if set(body) != {"provider", "model", "assistant_texts"}:
            raise HTTPException(422, "模型輸出證據欄位無效。")
        texts = body["assistant_texts"]
        if (not isinstance(body["provider"], str) or not 0 < len(body["provider"]) <= 64
                or not isinstance(body["model"], str) or not 0 < len(body["model"]) <= 128
                or not isinstance(texts, list) or len(texts) > 64
                or any(not isinstance(text, str) or len(text) > 100000 for text in texts)
                or sum(len(text) for text in texts) > 500000):
            raise HTTPException(422, "模型輸出證據格式無效。")
        lock = self.native_run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run, turn, capability = await self._native_authority(run_id)
            if run["state"] == "completed":
                return self._run_state(run)
            policy = (turn.get("prepared_context") or {}).get("policy", {})
            if body["provider"] != policy.get("provider", WECHAT_PROVIDER) or body["model"] != policy.get("model", WECHAT_MODEL):
                return await self._interrupt_native_run(run, turn, "native_provider_mismatch")
            candidates = {text.strip() for text in texts if text.strip()}
            await self.record_stage(turn["id"], capability, "model_output", "模型已返回內容，正在完成回覆")
            for separator in ("", "\n", "\n\n"):
                combined = separator.join(texts).strip()
                if combined:
                    candidates.add(combined)
            run = await self.db("update_native_run", run_id, output_seen=True,
                                output_hashes=sorted(digest(text) for text in candidates), provider=body["provider"], model=body["model"])
            return await self._join_native_run(run, turn, capability)

    async def native_end(self, run_id, body):
        self._run_id(run_id)
        if set(body) != {"success", "text"} or type(body["success"]) is not bool or not isinstance(body["text"], str) or len(body["text"]) > 100000:
            raise HTTPException(422, "執行完成證據格式無效。")
        lock = self.native_run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run, turn, capability = await self._native_authority(run_id)
            if run["state"] == "completed":
                return self._run_state(run)
            run = await self.db("update_native_run", run_id, end_seen=True, success=body["success"], terminal_text=body["text"].strip())
            if not body["success"]:
                return await self._interrupt_native_run(run, turn, "native_run_failed")
            return await self._join_native_run(run, turn, capability)

    async def _interrupt_native_run(self, run, turn, reason):
        run = await self.db("update_native_run", run["run_id"], state="interrupted")
        try:  # 不再靜默失敗：告訴用戶這條沒有處理成功
            if await self.db("notify", turn["id"], self.FAILURE_TEXT):
                self.nudge_outbox()
        except Exception:
            pass
        await self.suspend_turn(turn["id"], turn["owner"], reason=reason,
                                expected_capability=self.native_run_caps.get(run["run_id"]), expected_attempt=run["attempt"])
        return self._run_state(run)

    async def _join_native_run(self, run, turn, capability):
        if not run["end_seen"] or not run["output_seen"]:
            return self._run_state(run)
        if not run.get("success") or not run.get("terminal_text") or digest(run["terminal_text"]) not in run.get("output_hashes", []):
            return await self._interrupt_native_run(run, turn, "native_completion_mismatch")
        # Recheck the original capability immediately before the only business
        # submission. The per-run lock prevents both callbacks submitting it.
        await self._native_authority(run["run_id"])
        await self.db("update_native_run", run["run_id"], state="committing")
        try:
            await self.commit(turn["id"], capability, {"text": run["terminal_text"],
                              "advancement_key": "accepted-run-" + run["run_id"], "artifacts": []})
        except Exception:
            return await self._interrupt_native_run(run, turn, "native_commit_interrupted")
        run = await self.db("update_native_run", run["run_id"], state="completed")
        return self._run_state(run)

    async def connection(self, owner):
        await self.recover()
        binding = await self.db("binding", owner)
        available, reason = False, "本機微信服務尚未配置。"
        status = {}
        self.refresh_local_token()
        if self.token and getattr(self.host, "wechat_chat", None) is not None:
            try:
                status = await self.broker("GET", "/status")
                available = bool(status.get("available", status.get("ready", status.get("running", False))))
                reason = "" if available else "本機 OpenClaw 微信服務未就緒。"
            except HTTPException:
                if self.auto_manage_gateway and await self.ensure_gateway_running(wait_seconds=15.0):
                    try:
                        status = await self.broker("GET", "/status")
                        available = bool(status.get("available", status.get("ready", status.get("running", False))))
                        reason = "" if available else "本機 OpenClaw 微信服務未就緒。"
                    except HTTPException:
                        reason = "本機 OpenClaw 微信服務未啟動。"
                else:
                    reason = "本機 OpenClaw 微信服務未啟動。"
        health = "unknown"
        if binding and binding["state"] == "connected":
            account_status = next((item for item in status.get("accounts", [])
                                   if isinstance(item, dict) and item.get("account_id") == binding["account_id"]), {})
            if account_status.get("auth_expired") is True:
                return await self.disconnect(owner, reason="auth_expired")
            if not available or account_status.get("online") is False:
                health = "offline"
                if available:
                    reason = "微信連接暫時中斷，正在重新連接。"
            elif account_status.get("online") is True:
                health = "online"
        revoked = bool(binding and binding.get("revoked_reason") == "auth_expired")
        if revoked:
            reason = "微信授權已失效，請重新連接。"
        return dict(state="revoked" if revoked else binding["state"] if binding else "unbound", account=owner,
                    connection_health=health,
                    model=WECHAT_MODEL_NAME, identity=binding.get("identity") if binding else None,
                    conversation_id=binding.get("conversation_id") if binding else None,
                    skill_id=binding.get("skill_id", "none") if binding else "none",
                    force_web=bool(binding.get("force_web")) if binding else False,
                    force_think=bool(binding.get("force_think")) if binding else False,
                    capabilities={"text": True, "current_message_attachments": True, "library_auto_import": False},
                    available=available and owner != "0000", reason="展示帳號不能綁定私人微信。" if owner == "0000" else reason)

    async def update_request(self, owner, request_id, result):
        allowed_states = {"pending", "waiting_scan", "waiting_confirmation", "pin_required", "choice_required", "verified", "expired", "failed", "conflict"}
        binding_errors = {
            "upstream_unreachable": "本機微信服務無法連線到微信伺服器，二維碼未能生成。請檢查網絡或代理設定後重新取得。",
            "qr_unavailable": "微信伺服器暫時沒有返回二維碼，請稍後重新取得。",
            "already_bound_requires_reconnect": "這個微信已有連接，請重新掃碼確認。",
        }
        state = result.get("state", "waiting_scan")
        if state not in allowed_states:
            raise HTTPException(502, "微信服務返回未知的授權狀態。")
        fields = {key: result[key] for key in ("state", "qr_url", "qr_data_url", "verification_kind", "choices") if key in result}
        fields["state"] = state
        if result.get("error"):
            fields["error"] = binding_errors.get(result["error"], "微信授權未完成，請重新嘗試。")
        if isinstance(result.get("expires_at"), (int, float)):
            current = await self.db("request", request_id, owner)
            fields["expires_at"] = min(result["expires_at"], current["expires_at"])
        if state == "verified":
            account_id, sender_id = result.get("account_id"), result.get("sender_id")
            if not all(isinstance(item, str) and 0 < len(item) <= 512 for item in (account_id, sender_id)):
                raise HTTPException(502, "微信授權未返回可驗證的身份。")
            fields.update(account_id=account_id, sender_id=sender_id,
                          identity={"label": sender_id, "account_id": account_id, "sender_id": sender_id})
        if fields.get("qr_url") and not fields.get("qr_data_url"):
            try:
                import qrcode
                buffer = io.BytesIO()
                qrcode.make(fields["qr_url"]).save(buffer, format="PNG")
                fields["qr_data_url"] = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
            except ImportError:
                pass
        return await self.db("update_request", request_id, owner, **fields)

    async def create_binding(self, owner):
        if owner == "0000":
            raise HTTPException(403, "展示帳號不能綁定私人微信。")
        if not (await self.connection(owner))["available"]:
            raise HTTPException(503, "本機微信服務未就緒，暫時不能建立連接。")
        request = await self.db("new_request", owner)
        try:
            result = await self.broker("POST", "/binding/start", {"request_id": request["id"]})
            return await self.update_request(owner, request["id"], result)
        except HTTPException:
            await self.db("update_request", request["id"], owner, state="failed", error="本機微信授權啟動失敗，請重試。")
            raise

    async def binding_request(self, owner, request_id):
        request = await self.db("request", request_id, owner)
        if request["state"] in {"pending", "waiting_scan", "waiting_confirmation", "pin_required", "choice_required"}:
            result = await self.broker("GET", "/binding/" + request_id)
            request = await self.update_request(owner, request_id, result)
        return request

    async def confirm(self, owner, request_id):
        async with self.binding_locks.setdefault(owner, asyncio.Lock()):
            request = await self.db("request", request_id, owner)
            if request["state"] != "confirmed":
                if request["state"] != "verified":
                    raise HTTPException(409, "請先完成微信授權，核對身份後再確認。")
                conversation = await self.chat.ensure_conversation(owner)
                binding = await self.db("confirm", request_id, owner, conversation["id"])
                try:
                    await self.broker("POST", "/binding/" + request_id + "/confirm",
                                      {"account_id": binding["account_id"], "sender_id": binding["sender_id"]})
                except HTTPException:
                    await self.db("disconnect", owner)
                    raise
        # Health may revoke an expired authorization and acquire the same lock.
        return await self.connection(owner)

    async def release(self, turn_id, *, abort=False):
        worker = self.progress_workers.pop(turn_id, None)
        if worker:
            worker.cancel()
        forwarded = self.web_tasks.get(turn_id)
        if forwarded and forwarded is not asyncio.current_task() and not forwarded.done():
            forwarded.cancel()  # 暫停或解除連接：停止仍在網站流水線中的回合
        active = self.active.pop(turn_id, None)
        if active:
            owner, cid = active
            try:
                if abort:
                    await self.chat.abort_turn(owner, cid, turn_id=turn_id)
            finally:
                await self.chat.release_turn(owner, cid, turn_id)

    async def disconnect(self, owner, reason=None):
        async with self.binding_locks.setdefault(owner, asyncio.Lock()):
            binding = await self.db("binding", owner)
            await self.db("disconnect", owner, reason=reason)
            for key, value in list(self.active.items()):
                if value[0] == owner:
                    await self.release(key, abort=True)
            if binding:
                try:
                    await self.broker("POST", "/connections/disconnect",
                                      {"account_id": binding["account_id"], "sender_id": binding["sender_id"]})
                except HTTPException:
                    pass  # Local revocation remains authoritative even while the gateway is unavailable.
        return await self.connection(owner)

    async def new_turn(self, body):
        allowed = {"event_id", "account_id", "sender_id", "text", "file_ids", "attachments"}
        if set(body) - allowed:
            raise HTTPException(422, "微信訊息欄位無效。")
        for key in ("event_id", "account_id", "sender_id"):
            if not isinstance(body.get(key), str) or not 0 < len(body[key]) <= 512:
                raise HTTPException(422, "微信訊息身份不完整。")
        text = body.get("text", "")
        if not isinstance(text, str) or len(text) > 2000:
            raise HTTPException(422, "微信訊息最多 2000 字。")
        file_ids, attachments = body.get("file_ids", []), body.get("attachments", [])
        if not isinstance(file_ids, list) or len(file_ids) > 12 or any(not isinstance(item, str) or len(item) > 128 for item in file_ids):
            raise HTTPException(422, "檔案引用無效。")
        if not isinstance(attachments, list) or len(attachments) + len(file_ids) > 12:
            raise HTTPException(422, "一次最多接收 12 個附件。")
        total = 0
        for attachment in attachments:
            if not isinstance(attachment, dict) or set(attachment) - {"name", "mime", "content_b64"}:
                raise HTTPException(422, "附件不能指定本機路徑或遠端網址。")
            raw = attachment.get("content_b64", "")
            if not isinstance(raw, str) or len(raw) > 70 * 1024 * 1024:
                raise HTTPException(413, "附件總大小不能超過 50 MB。")
            try:
                total += len(base64.b64decode(raw, validate=True))
            except (ValueError, TypeError):
                raise HTTPException(422, "附件內容格式無效。") from None
        if total > 50 * 1024 * 1024:
            raise HTTPException(413, "附件總大小不能超過 50 MB。")
        if not text.strip() and not attachments:
            raise HTTPException(422, "訊息不能為空。")
        binding = await self.db("identity", body["account_id"], body["sender_id"])
        if not binding:
            raise HTTPException(403, "這個微信尚未完成小智帳號綁定。")
        await self.chat.ensure_conversation(binding["owner"], binding["conversation_id"])
        # Include attachment digests in idempotency without persisting base64 in the inbox.
        fingerprint_ids = file_ids + ["attachment:" + digest(item.get("name", "") + ":" + item.get("content_b64", "")) for item in attachments]
        previous = await self.db("lookup_event", binding, body["event_id"], text.strip(), fingerprint_ids)
        if previous:
            return public_turn(previous)
        if text.strip() and not attachments and re.search(r"轉|转|翻譯|翻译|整理|總結|总结|編輯|编辑|生成|出題|出题|製作|制作|提取|解題|解题",text):
            pending = await self.db("tasks", binding["owner"], binding["conversation_id"], {"waiting_instruction"})
            pending = [item for item in pending if item["created_at"] >= time.time()-900 or re.search(r"上一份|剛才|刚才",text)]
            if pending:
                turn, capability = await self.db("attach_instruction",binding,pending[0]["id"],body["event_id"],text.strip(),fingerprint_ids)
                return public_turn(turn,capability,created=True)
        if attachments:
            waiting = await self.db("tasks", binding["owner"], binding["conversation_id"], {"waiting_attachment"})
            if waiting:
                target = waiting[0]
                staged = await self.chat.stage_attachments(binding["owner"], target["conversation_id"], target["id"], attachments)
                turn, capability = await self.db("attach_waiting", binding, target["id"], body["event_id"], text.strip(), fingerprint_ids, staged)
                return public_turn(turn, capability, created=True)
        turn, capability, created = await self.db("create_turn", binding, body["event_id"], text.strip(), fingerprint_ids)
        if created:
            try:
                staged = await self.chat.stage_attachments(binding["owner"], binding["conversation_id"], turn["id"], attachments) if attachments else []
                turn = await self.db("update_turn", turn["id"], file_ids=file_ids, attachments=staged)
                waiting_check = getattr(self.chat, "waiting_for_attachment", None)
                if not attachments and not file_ids and waiting_check and waiting_check(text):
                    turn = await self.db("update_turn", turn["id"], status="waiting_attachment", awaiting_attachment=True,
                                         stage="等待本次附件", updated_at=time.time())
                    capability = None
                if attachments and not text.strip() and any(Path(item.get("name","")).suffix.lower() in {".pdf",".docx",".xlsx",".pptx",".txt"} for item in attachments):
                    turn = await self.db("update_turn",turn["id"],status="waiting_instruction",awaiting_instruction=True,
                                         stage="附件已保存，等待處理要求",updated_at=time.time())
                    capability = None
            except Exception:
                await self.db("update_turn", turn["id"], status="aborted", expires_at=0, error="附件保存失敗，請重新發送。")
                raise
        return public_turn(turn, capability, created)

    async def context(self, turn_id, capability):
        async with self.turn_locks.setdefault(turn_id, asyncio.Lock()):
            turn = await self.db("turn", turn_id, capability)
            if turn["status"] == "running" and (turn.get("prepared_context") or {}).get("pipeline") == "web":
                return {"completed": True, "result": {"accepted": True}}
            if turn["status"] == "running" and turn.get("prepared_context"):
                return turn["prepared_context"]
            if turn["status"] != "queued":
                raise HTTPException(409, "回合目前不能開始。")
            if self.uses_web_pipeline(turn):
                # 後端自己完成整個回合並經 outbox 投遞；通道端不再啟動模型，也不必等待。
                await self.db("update_turn", turn_id, status="running", prepared_context={"pipeline": "web"},
                              expires_at=time.time() + 900)
                self.web_tasks[turn_id] = asyncio.create_task(self.run_web(turn, capability))
                return {"completed": True, "result": {"accepted": True}}
            await self.record_stage(turn_id, capability, "waiting_conversation", "正在等待本對話前一個處理完成")
            worker = self.progress_workers.get(turn_id)
            if worker is None or worker.done():
                self.progress_workers[turn_id] = asyncio.create_task(self.report_progress(turn_id, capability))
            try:
                await self.chat.acquire_turn(turn["owner"], turn["conversation_id"], turn_id)
            except BaseException:
                await self.release(turn_id)
                await self.db("update_turn", turn_id, status="interrupted", expires_at=0)
                raise
            self.active[turn_id] = (turn["owner"], turn["conversation_id"])
            if self.expiry_worker is None or self.expiry_worker.done():
                self.expiry_worker = asyncio.create_task(self.expire_active())
            try:
                await self.db("turn", turn_id, capability)
                prepared = await self.chat.prepare_context(turn["owner"], turn["conversation_id"], turn["text"],
                    turn_id=turn_id, skill_id=turn["skill_id"], file_ids=turn["file_ids"], attachments=turn["attachments"],
                    progress=lambda stage, message, **metadata: self.record_stage(turn_id, capability, stage, message, **metadata))
                # A pause may arrive during retrieval/preparation. It must not be
                # undone by a late context result renewing the retired lease.
                await self.db("turn", turn_id, capability)
                progress = await self.db("tool_progress", turn_id)
                if turn.get("attempt", 1) > 1:
                    resume = prepared.setdefault("resume", {})
                    resume.update(attempt=turn["attempt"], completed_tools=[{"call_id": item["call_id"], "name": item["name"]}
                        for item in progress if item["state"] == "completed"])
                    prepared["system_prompt"] += "\n已持久完成的工具步驟：" + json.dumps(resume, ensure_ascii=False) + "。請從未完成部分繼續，不要重複已完成步驟。"
                await self.db("update_turn", turn_id, status="running", prepared_context=prepared, expires_at=time.time() + 900)
                if prepared.get("direct_response") is not None:
                    direct = prepared["direct_response"]
                    async with self.binding_locks.setdefault(turn["owner"], asyncio.Lock()):
                        result = await self._commit(turn_id, capability, {"text": direct["text"],
                            "advancement_key": "original-file:" + turn_id + ":" + str(turn.get("attempt", 1)), "artifacts": []})
                    return {"completed": True, "result": result}
                await self.record_stage(turn_id, capability, "starting_model", "資料與上下文已準備，正在啟動盤古回覆")
                return prepared
            except BaseException:
                await self.release(turn_id, abort=True)
                await self.db("update_turn", turn_id, status="interrupted", expires_at=0)
                raise

    async def expire_active(self):
        try:
            while self.active:
                await asyncio.sleep(15)
                for turn_id in list(self.active):
                    turn = await self.db("turn", turn_id)
                    if turn["expires_at"] <= time.time() or turn["status"] in {"aborted", "interrupted"}:
                        await self.suspend_turn(turn_id, turn["owner"], reason="lease_expired")
        except asyncio.CancelledError:
            pass

    async def tool(self, turn_id, capability, name, arguments, call_id=None):
        turn = await self.db("turn", turn_id, capability)
        if turn["status"] != "running" or turn_id not in self.active:
            raise HTTPException(409, "回合尚未開始。")
        if not isinstance(arguments, dict) or any(key in arguments for key in {"owner", "user_id", "account_id", "sender_id", "receiver", "recipient", "path", "output_dir", "capability"}):
            raise HTTPException(422, "工具不能自行指定身份、收件人或本機路徑。")
        if call_id is None:
            from uuid import uuid4
            call_id = "legacy-" + uuid4().hex
        if not isinstance(call_id, str) or not 0 < len(call_id) <= 512:
            raise HTTPException(422, "工具呼叫編號無效。")
        receipt = await self.db("begin_tool", turn_id, call_id, name, arguments)
        if receipt["state"] == "completed":
            return receipt["result"]
        existing = self.running_tools.get(turn_id)
        if existing:
            if existing[0] != call_id:
                raise HTTPException(409, "本任務的上一個工具步驟仍在完成，請稍後繼續。")
            job = existing[1]
        else:
            async def work():
                labels = {"search_files": "正在查找有權使用的文件", "retrieve": "正在檢索文件原文依據",
                          "read_file": "正在讀取原文件", "inspect_file": "正在檢查原文件結構",
                          "create_file": "正在建立文件", "edit_file": "正在修改文件", "validate_file": "正在檢查文件完整性",
                          "list_tasks": "正在查詢已保存任務"}
                await self.record_stage(turn_id, capability, "tool_" + name, labels.get(name, "正在執行文件處理步驟"))
                result = await self.chat.execute_tool(turn["owner"], turn["conversation_id"], name, arguments, turn_id=turn_id, call_id=call_id)
                await self.db("finish_tool", turn_id, call_id, result)
                entries = await self.db("tool_progress", turn_id)
                current = await self.db("turn", turn_id)
                await self.db("update_turn", turn_id, progress={"completed": sum(item["state"] == "completed" for item in entries), "total": None, "unit": "步"},
                              stage="已暫停，可繼續" if current["status"] == "interrupted" else "已保存完成步驟", updated_at=time.time())
                if current["status"] == "running":
                    await self.record_stage(turn_id, capability, "generating", "工具步驟已完成，盤古正在整理回覆",
                        completed_steps=["已完成 %s 個工具步驟" % sum(item["state"] == "completed" for item in entries)])
                return result
            job = asyncio.create_task(work())
            self.running_tools[turn_id] = (call_id, job)
            def settled(task):
                if self.running_tools.get(turn_id, (None, None))[1] is task:
                    self.running_tools.pop(turn_id, None)
                if not task.cancelled():
                    task.exception()
            job.add_done_callback(settled)
        try:
            result = await asyncio.shield(job)
            await self.db("turn", turn_id, capability)
            return result
        except asyncio.CancelledError:
            await self.suspend_turn(turn_id, turn["owner"], reason="request_interrupted")
            raise

    async def _settle_release(self, turn_id, attempt):
        running = self.running_tools.get(turn_id)
        if running:
            try:
                await asyncio.shield(running[1])
            except BaseException:
                pass
        current = await self.db("turn", turn_id)
        if current.get("attempt", 1) == attempt and current["status"] in {"interrupted", "aborted"}:
            await self.release(turn_id, abort=True)

    async def suspend_turn(self, turn_id, owner, reason="interrupted", *, expected_capability=None, expected_attempt=None):
        async with self.binding_locks.setdefault(owner, asyncio.Lock()):
            if expected_attempt is not None:
                current = await self.db("turn", turn_id, expected_capability or "")
                if current.get("attempt", 1) != expected_attempt:
                    raise HTTPException(409, "舊的原生回調不能暫停新的執行批次。")
            turn = await self.db("suspend", turn_id, owner, reason)
        if turn["status"] == "completed":
            return await self.task_public(turn)
        if turn_id in self.running_tools:
            task = asyncio.create_task(self._settle_release(turn_id, turn.get("attempt", 1)))
            self.settlers.add(task)
            task.add_done_callback(self.settlers.discard)
        else:
            await self.release(turn_id, abort=True)
        try:
            await self.broker("POST", "/tasks/suspend", {"turn_id": turn_id, "account_id": turn["account_id"], "sender_id": turn["sender_id"], "attempt": turn.get("attempt", 1)})
        except HTTPException:
            pass
        return await self.task_public(turn)

    async def task_public(self, turn):
        progress = turn.get("progress") or {"completed": 0, "total": None, "unit": "步"}
        return {"id": turn["id"], "state": turn["status"], "stage": turn.get("stage") or {"queued": "等待處理", "running": "正在處理", "completed": "已完成", "interrupted": "已暫停，可繼續"}.get(turn["status"], "等待處理"),
                "progress": progress, "resumable": turn["status"] in {"interrupted", "aborted"}, "conversation_id": turn["conversation_id"],
                "attachment_count": len(turn.get("attachments", [])), "output_count": len((turn.get("result") or {}).get("artifacts", [])),
                "created_at": turn["created_at"], "updated_at": turn.get("updated_at", turn["created_at"])}

    async def resume_turn(self, turn_id, body):
        async with self.turn_locks.setdefault(turn_id, asyncio.Lock()):
            return await self._resume_turn(turn_id, body)

    async def _resume_turn(self, turn_id, body):
        binding = await self.verified_binding(body)
        turn = await self.db("owned_turn", turn_id, binding["owner"])
        running = self.running_tools.get(turn_id)
        if running:
            try:
                await asyncio.shield(running[1])
            except BaseException:
                pass
        await self.release(turn_id, abort=True)
        await self.chat.ensure_conversation(turn["owner"], turn["conversation_id"])
        async with self.binding_locks.setdefault(binding["owner"], asyncio.Lock()):
            turn, token = await self.db("resume", turn_id, binding)
        return public_turn(turn, token if turn["status"] == "queued" else None, created=True)

    async def verified_binding(self, body):
        if set(body) - {"account_id", "sender_id", "event_id", "action"}:
            raise HTTPException(422, "任務控制欄位無效。")
        if any(not isinstance(body.get(key), str) or not body[key] for key in ("account_id", "sender_id")):
            raise HTTPException(422, "缺少已驗證的微信身份。")
        binding = await self.db("identity", body["account_id"], body["sender_id"])
        if not binding:
            raise HTTPException(403, "微信連接已失效。")
        return binding

    async def queue_resume(self, turn, binding):
        if turn["owner"] != binding["owner"]:
            raise HTTPException(404, "未找到本人任務。")
        if turn["status"] not in {"interrupted", "aborted"}:
            raise HTTPException(409, "這個任務目前不能繼續。")
        if turn["epoch"] != binding["epoch"] or turn["account_id"] != binding["account_id"] or turn["sender_id"] != binding["sender_id"]:
            raise HTTPException(403, "原任務的微信授權已撤銷。")
        if turn["conversation_id"] != binding["conversation_id"]:
            raise HTTPException(409, "請先在微信連接設定選擇任務的原會話。")
        await self.broker("POST", "/tasks/resume", {"turn_id": turn["id"], "account_id": binding["account_id"], "sender_id": binding["sender_id"]})
        public = await self.task_public(turn)
        public.update(state="resuming", stage="已排隊繼續處理")
        return public

    async def control(self, body):
        binding = await self.verified_binding(body)
        event_id, action = body.get("event_id"), body.get("action")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 512 or action not in {"pause", "resume"}:
            raise HTTPException(422, "任務控制指令無效。")
        previous = await self.db("control", binding, event_id)
        if previous:
            return previous
        statuses = {"queued", "running", "waiting_attachment", "waiting_instruction"} if action == "pause" else {"interrupted", "aborted"}
        turns = await self.db("tasks", binding["owner"], binding["conversation_id"], statuses)
        if not turns:
            raise HTTPException(404, "目前沒有可暫停或繼續的本人任務。")
        result = await self.suspend_turn(turns[0]["id"], binding["owner"], reason="user_pause") if action == "pause" else await self.queue_resume(turns[0], binding)
        await self.db("control", binding, event_id, result)
        return result

    async def commit(self, turn_id, capability, body):
        async with self.turn_locks.setdefault(turn_id, asyncio.Lock()):
            turn = await self.db("turn", turn_id, capability, allow_completed=True)
            async with self.binding_locks.setdefault(turn["owner"], asyncio.Lock()):
                return await self._commit(turn_id, capability, body)

    async def _commit(self, turn_id, capability, body):
        turn = await self.db("turn", turn_id, capability, allow_completed=True)
        text, advancement = body.get("text"), body.get("advancement_key")
        artifacts = body.get("artifacts", [])
        if set(body) - {"text", "advancement_key", "artifacts"} or not isinstance(text, str) or not text.strip() or len(text) > 100000 or not isinstance(advancement, str) or not 0 < len(advancement) <= 512:
            raise HTTPException(422, "回答提交內容無效。")
        if not isinstance(artifacts, list) or len(artifacts) > 8:
            raise HTTPException(422, "回答附件無效。")
        # Persist and deliver the same canonical text used by the business helper.
        text = text.strip()
        if turn["status"] == "completed":
            expected = digest(json.dumps([advancement, text, artifacts], sort_keys=True, ensure_ascii=False))
            if turn.get("commit_fingerprint") != expected:
                raise HTTPException(409, "同一回合不能提交不同的回答。")
            return turn["result"]
        if turn["status"] != "running" or turn_id not in self.active:
            raise HTTPException(409, "回合尚未開始或已中斷。")
        try:
            await self.record_stage(turn_id, capability, "saving", "正在核對附件權限並保存回覆")
            await self.db("begin_commit", turn_id, advancement, text, artifacts)
            result = await self.chat.commit_reply(turn["owner"], turn["conversation_id"], text,
                turn_id=turn_id, reply_id=turn["reply_id"], prepared_context=turn["prepared_context"], artifacts=artifacts)
            # Only backend-validated artifact metadata, never caller paths/names, enters delivery.
            validated = result.get("artifacts", [])
            canonical = canonical_reply_text(text, turn.get("prepared_context"))
            if result.get("text", text) != canonical:
                raise HTTPException(409, "已保存的回覆與本輪原文件結果不一致。")
            saved = await self.db("finish", turn_id, advancement, canonical, validated, result,
                request_fingerprint=digest(json.dumps([advancement, text, artifacts], sort_keys=True, ensure_ascii=False)))
            return saved["result"]
        except BaseException:
            current = await self.db("turn", turn_id)
            if current["status"] != "completed":
                await self.db("update_turn", turn_id, status="interrupted", expires_at=0)
            raise
        finally:
            await self.release(turn_id)

    async def abort(self, turn_id, capability):
        turn = await self.db("turn", turn_id, capability, allow_completed=True)
        if turn["status"] != "completed":
            return await self.suspend_turn(turn_id, turn["owner"], reason="native_interrupted")
        return {"id": turn_id, "state": "completed"}

    async def stop(self):
        self.auto_manage_gateway = False
        if self.gateway_supervisor_task:
            self.gateway_supervisor_task.cancel()
            try:
                await self.gateway_supervisor_task
            except asyncio.CancelledError:
                pass
            self.gateway_supervisor_task = None
        workers = [*self.progress_workers.values(), *self.web_tasks.values()]
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self.progress_workers.clear()
        if self.expiry_worker:
            self.expiry_worker.cancel()
            await self.expiry_worker
        for _, job in list(self.running_tools.values()):
            try:
                await asyncio.shield(job)
            except BaseException:
                pass
        for turn_id in list(self.active):
            try:
                await self.release(turn_id, abort=True)
                await self.db("update_turn", turn_id, status="interrupted", expires_at=0)
            except Exception:
                pass

    async def recover(self):
        """Reconcile the JSON/SQLite write boundary using trusted stable reply IDs."""
        if self.recovered:
            return
        async with self.recovery_lock:
            if self.recovered:
                return
            for turn in await self.db("interrupted_commits"):
                conversation = await self.host.data_store.get_conversation(turn["conversation_id"])
                if not conversation or conversation.get("user_id") != turn["owner"]:
                    continue
                message = next((message for message in conversation.get("messages", [])
                                if message.get("id") == turn["reply_id"] and not message.get("is_user")), None)
                payload = turn["commit_payload"]
                expected_text = canonical_reply_text(payload["text"], turn.get("prepared_context"))
                if not message or message.get("content") != expected_text or message.get("answer_status") == "interrupted":
                    continue
                artifacts = message.get("artifacts", [])
                result = {"reply_id": turn["reply_id"], "message_id": turn["reply_id"],
                          "conversation_id": turn["conversation_id"], "text": message["content"],
                          "artifacts": artifacts, "citation_map": message.get("citation_map", []),
                          "provider": message.get("provider", WECHAT_PROVIDER), "model": message.get("model", WECHAT_MODEL)}
                try:
                    await self.db("finish", turn["id"], payload["advancement_key"], message["content"], artifacts,
                                  result, request_fingerprint=turn["commit_fingerprint"], reconcile=True)
                except HTTPException:
                    continue  # A revoked binding must never have its old delivery resurrected.
            self.recovered = True


def get_controller(host):
    existing = getattr(host.app.state, "wechat_controller", None)
    root = Path(host.DATA_DIR).resolve()
    source = Path(__file__).resolve().parents[1]
    conv_root = Path(getattr(host, "CONVERSATIONS_FILE", root / "conversations_data.json")).resolve().parent
    if root == source and conv_root != source:
        root = conv_root
    if existing and existing.store.path.resolve() == (root / "storage" / "wechat.sqlite3").resolve():
        return existing
    controller = WechatController(host, root=root)
    host.app.state.wechat_controller = controller
    return controller


def install(host):
    app = host.app
    if getattr(app.state, "wechat_routes_installed", False):
        return
    app.state.wechat_routes_installed = True
    def get():
        return get_controller(host)
    def ok(value):
        return {"code": 200, "data": value}

    @app.get("/api/wechat/connection")
    async def connection(session_id: str = Depends(host.verify_session)):
        return ok(await get().connection(await get().owner(session_id)))

    @app.get("/api/wechat/tasks")
    async def tasks(session_id: str = Depends(host.verify_session)):
        owner = await get().owner(session_id)
        rows = await get().db("tasks", owner)
        return ok({"tasks": [await get().task_public(row) for row in rows[:30]]})

    @app.post("/api/wechat/tasks/{turn_id}/suspend")
    async def suspend_task(turn_id: str, session_id: str = Depends(host.verify_session)):
        owner = await get().owner(session_id)
        await get().db("owned_turn", turn_id, owner)
        return ok(await get().suspend_turn(turn_id, owner, reason="user_pause"))

    @app.post("/api/wechat/tasks/{turn_id}/resume")
    async def resume_task(turn_id: str, session_id: str = Depends(host.verify_session)):
        owner = await get().owner(session_id)
        turn = await get().db("owned_turn", turn_id, owner)
        binding = await get().db("binding", owner)
        if not binding or binding["state"] != "connected":
            raise HTTPException(409, "請先連接本人微信。")
        return ok(await get().queue_resume(turn, binding))

    @app.post("/api/wechat/bind-requests")
    async def create_binding(session_id: str = Depends(host.verify_session)):
        return ok(public_request(await get().create_binding(await get().owner(session_id))))

    @app.get("/api/wechat/bind-requests/{request_id}")
    async def binding_request(request_id: str, session_id: str = Depends(host.verify_session)):
        return ok(public_request(await get().binding_request(await get().owner(session_id), request_id)))

    @app.delete("/api/wechat/bind-requests/{request_id}")
    async def cancel_binding(request_id: str, session_id: str = Depends(host.verify_session)):
        owner = await get().owner(session_id)
        request = await get().db("request", request_id, owner)
        if request["state"] != "confirmed":
            request = await get().db("update_request", request_id, owner, state="cancelled", qr_url=None, qr_data_url=None)
            try:
                await get().broker("DELETE", "/binding/" + request_id)
            except HTTPException:
                pass
        return ok(public_request(request))

    @app.post("/api/wechat/bind-requests/{request_id}/verification")
    async def verification(request_id: str, body: dict = Body(...), session_id: str = Depends(host.verify_session)):
        owner = await get().owner(session_id)
        request = await get().db("request", request_id, owner)
        if set(body) - {"pin", "choice"} or not body or request["state"] not in {"pin_required", "choice_required", "waiting_confirmation"}:
            raise HTTPException(422, "目前不需要這項授權步驟。")
        if "pin" in body and (not isinstance(body["pin"], str) or not re.fullmatch(r"[A-Za-z0-9 -]{1,32}", body["pin"])):
            raise HTTPException(422, "驗證碼格式無效。")
        if "choice" in body and body["choice"] not in {"own", "delegate"}:
            raise HTTPException(422, "請選擇有效的授權方式。")
        result = await get().broker("POST", "/binding/" + request_id + "/verify", body)
        return ok(public_request(await get().update_request(owner, request_id, result)))

    @app.post("/api/wechat/bind-requests/{request_id}/confirm")
    async def confirm(request_id: str, session_id: str = Depends(host.verify_session)):
        return ok(await get().confirm(await get().owner(session_id), request_id))

    @app.delete("/api/wechat/connection")
    async def disconnect(session_id: str = Depends(host.verify_session)):
        return ok(await get().disconnect(await get().owner(session_id)))

    @app.get("/api/wechat/conversations")
    async def conversations(session_id: str = Depends(host.verify_session)):
        owner = await get().owner(session_id)
        rows = await host.data_store.get_user_conversations(owner)
        return ok({"conversations": [{"id": row["id"], "title": row.get("title", "新對話")} for row in rows]})

    @app.put("/api/wechat/conversation")
    async def conversation(body: dict = Body(...), session_id: str = Depends(host.verify_session)):
        owner = await get().owner(session_id)
        if set(body) != {"conversation_id"} or not isinstance(body["conversation_id"], str):
            raise HTTPException(422, "請選擇本人對話。")
        record = await host.data_store.get_conversation(body["conversation_id"])
        if not record or record.get("user_id") != owner:
            raise HTTPException(404, "未找到本人對話。")
        await get().db("patch_binding", owner, conversation_id=record["id"])
        return ok(await get().connection(owner))

    @app.patch("/api/wechat/preferences")
    async def preferences(body: dict = Body(...), session_id: str = Depends(host.verify_session)):
        owner = await get().owner(session_id)
        if not body or set(body) - {"skill_id", "force_web", "force_think"}:
            raise HTTPException(422, "微信偏好設定欄位無效。")
        if "skill_id" in body and body["skill_id"] not in {"none", "chemistry-teacher"}:
            raise HTTPException(422, "教學模式無效。")
        if any(key in body and type(body[key]) is not bool for key in ("force_web", "force_think")):
            raise HTTPException(422, "聯網與思考開關只能是開或關。")
        await get().db("patch_binding", owner, **body)
        return ok(await get().connection(owner))

    async def trusted(x_houkong_wechat_token: str | None = Header(None)):
        get().internal(x_houkong_wechat_token)
        await get().recover()

    @app.post("/internal/wechat/native-commits")
    async def native_commit(body: dict = Body(...), x_houkong_wechat_token: str | None = Header(None)):
        # Native advancement can be replayed before a fresh business turn.
        # Authenticate the profile only; do not recover, select or commit a turn.
        get().internal(x_houkong_wechat_token)
        return ok(await get().db("native_commit", body))

    async def capability(x_wechat_turn_capability: str | None = Header(None)):
        if not x_wechat_turn_capability:
            raise HTTPException(403, "缺少回合授權。")
        return x_wechat_turn_capability

    async def native_service(x_houkong_wechat_token: str | None = Header(None)):
        get().internal(x_houkong_wechat_token)

    @app.post("/internal/wechat/turns/{turn_id}/run-start", dependencies=[Depends(native_service)])
    async def native_run_start(turn_id: str, body: dict = Body(...), token: str = Depends(capability)):
        return ok(await get().native_run_start(turn_id, token, body))

    @app.post("/internal/wechat/runs/{run_id}/output", dependencies=[Depends(native_service)])
    async def native_output(run_id: str, body: dict = Body(...)):
        return ok(await get().native_output(run_id, body))

    @app.post("/internal/wechat/runs/{run_id}/end", dependencies=[Depends(native_service)])
    async def native_end(run_id: str, body: dict = Body(...)):
        return ok(await get().native_end(run_id, body))

    @app.post("/internal/wechat/authorize-inbound", dependencies=[Depends(trusted)])
    async def authorize_inbound(body: dict = Body(...)):
        # The pinned Tencent adapter calls this before native dispatch. Keeping
        # this gate outside the optional bridge hooks prevents legacy fallback
        # from reading old runtime transcripts when the bridge plugin is absent.
        if set(body) != {"account_id", "sender_id"} or any(
                not isinstance(body.get(key), str) or not 0 < len(body[key]) <= 512
                for key in ("account_id", "sender_id")):
            raise HTTPException(422, "微信通道身份無效。")
        binding = await get().db("identity", body["account_id"], body["sender_id"])
        if not binding:
            raise HTTPException(403, "微信尚未完成小智綁定。")
        status = await get().broker("GET", "/status")
        if not status.get("available", status.get("ready", False)):
            raise HTTPException(503, "微信業務接入尚未就緒。")
        return ok({"authorized": True})

    @app.post("/internal/wechat/turns", dependencies=[Depends(trusted)])
    async def new_turn(body: dict = Body(...)):
        return ok(await get().new_turn(body))

    @app.post("/internal/wechat/control", dependencies=[Depends(trusted)])
    async def control(body: dict = Body(...)):
        return ok(await get().control(body))

    @app.post("/internal/wechat/turns/{turn_id}/resume", dependencies=[Depends(trusted)])
    async def resume_turn(turn_id: str, body: dict = Body(...)):
        return ok(await get().resume_turn(turn_id, body))

    @app.post("/internal/wechat/turns/{turn_id}/suspend", dependencies=[Depends(trusted)])
    async def suspend_turn(turn_id: str, token: str = Depends(capability)):
        turn = await get().db("turn", turn_id, token, allow_completed=True)
        return ok(await get().suspend_turn(turn_id, turn["owner"]))

    @app.post("/internal/wechat/turns/{turn_id}/context", dependencies=[Depends(trusted)])
    async def context(turn_id: str, token: str = Depends(capability)):
        return ok(await get().context(turn_id, token))

    @app.post("/internal/wechat/turns/{turn_id}/tools/{name}", dependencies=[Depends(trusted)])
    async def tool(turn_id: str, name: str, body: dict = Body(...), token: str = Depends(capability)):
        if "arguments" not in body or set(body) - {"arguments", "call_id", "tool_call_id"}:
            raise HTTPException(422, "工具參數格式無效。")
        if "call_id" in body and "tool_call_id" in body and body["call_id"] != body["tool_call_id"]:
            raise HTTPException(422, "工具呼叫編號不一致。")
        return ok(await get().tool(turn_id, token, name, body["arguments"], body.get("call_id", body.get("tool_call_id"))))

    @app.post("/internal/wechat/turns/{turn_id}/commit", dependencies=[Depends(trusted)])
    async def commit(turn_id: str, body: dict = Body(...), token: str = Depends(capability)):
        return ok(await get().commit(turn_id, token, body))

    @app.post("/internal/wechat/turns/{turn_id}/abort", dependencies=[Depends(trusted)])
    async def abort(turn_id: str, token: str = Depends(capability)):
        return ok(await get().abort(turn_id, token))

    @app.post("/internal/wechat/turns/{turn_id}/heartbeat", dependencies=[Depends(trusted)])
    async def heartbeat(turn_id: str, token: str = Depends(capability)):
        turn = await get().db("turn", turn_id, token)
        if turn["status"] not in {"queued", "running"}:
            raise HTTPException(409, "回合已結束。")
        await get().db("update_turn", turn_id, expires_at=time.time() + 900)
        return ok({"id": turn_id, "state": turn["status"]})

    @app.get("/internal/wechat/outbox", dependencies=[Depends(trusted)])
    async def outbox(limit: int = Query(10, ge=1, le=20)):
        return ok({"items": await get().db("claim_outbox", limit)})

    @app.post("/internal/wechat/outbox/{item_id}/ack", dependencies=[Depends(trusted)])
    async def ack(item_id: str, body: dict = Body(...)):
        if set(body) - {"delivery_token", "status", "error"} or body.get("status") not in {"sent", "failed"}:
            raise HTTPException(422, "投遞狀態無效。")
        item = await get().db("ack", item_id, body.get("delivery_token"), body["status"], body.get("error"))
        return ok({"id": item["id"], "status": item["status"]})

    @app.post("/internal/wechat/outbox/{item_id}/authorize", dependencies=[Depends(trusted)])
    async def authorize_delivery(item_id: str, body: dict = Body(...)):
        if set(body) != {"delivery_token"}:
            raise HTTPException(422, "投遞授權格式無效。")
        item = await get().db("authorize_delivery", item_id, body.get("delivery_token"))
        return ok({"authorized": True, "target": item["target"]})

    @app.get("/internal/wechat/outbox/{item_id}/files/{file_id}", dependencies=[Depends(trusted)])
    async def artifact(item_id: str, file_id: str, x_wechat_delivery_token: str = Header(...)):
        item = await get().db("authorize_delivery", item_id, x_wechat_delivery_token)
        allowed_ids = {a.get("id", a.get("file_id")) for a in item.get("artifacts", []) if isinstance(a, dict)}
        if file_id not in allowed_ids:
            raise HTTPException(404, "未找到附件。")
        record = await host.data_store.find_file_by_id(file_id)
        if not record or not host.check_file_permission(record, item["owner"]):
            raise HTTPException(403, "附件權限已失效。")
        path = host.resolve_storage_path(record.get("storage_path"), record)
        if not path or not Path(path).is_file():
            raise HTTPException(410, "附件原件已不存在。")
        return FileResponse(path, filename=record.get("original_filename") or record.get("name") or "attachment")
