"""Lifecycle of an interactive browser chat turn."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from services.chat_learning import valid_chat_messages
from services.process_trace import ProcessTracker


active_turns = {}
PAUSED = {"stopped", "paused", "waiting"}


def event(data):
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def clarification_question(value):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:240]


class BrowserTurn:
    def __init__(self, host, request, session_id, owner, conversation_id):
        self.host, self.request, self.session_id = host, request, session_id
        self.owner, self.cid, self.id = owner, conversation_id, str(request.turn_id)
        self.user_id = str(uuid4())
        self.reply_id = str(uuid4())
        self.kind = "chat"
        self.plan = None
        self.work = None
        self.worker = None
        self.action = None
        self.visible_length = None
        self.committing = False
        self.finished = False
        self.completed = False
        self.question = ""
        self.task_id = None
        self.replaced_task_id = None
        self.resolved_files = None
        self.has_checkpoint = False
        self.previous = None
        self.supplement = ""
        self.supplements = []
        self.text = ""
        self.metadata = {}
        self.process_tracker = ProcessTracker()

    def begin_commit(self):
        if self.action:
            raise asyncio.CancelledError
        self.committing = True

    def stop(self, action, visible_length=None):
        if self.finished:
            return "finished"
        if self.committing:
            return "committing"
        if not self.action:
            self.action, self.visible_length = action, visible_length
            if self.worker and not self.worker.done():
                self.worker.cancel()
        return "stopping"

    def clarify(self, question):
        self.question = clarification_question(question)

    async def prepare(self):
        anchor = self.request.resume_from or self.request.clarification_reply_to
        if not anchor:
            return
        convo = await self.host.data_store.get_conversation(self.cid)
        messages = convo.get("messages", []) if convo and convo["user_id"] == self.owner else []
        previous = next((item for item in reversed(messages) if item.get("id") == anchor), {})
        marker = previous.get("turn_control", {})
        saved = previous.get("_web_request")
        already_used = any(item.get("_web_parent") == anchor for item in messages)
        expected = {"waiting"} if self.request.clarification_reply_to else {"stopped", "paused"}
        visible = valid_chat_messages(messages)
        if (marker.get("status") not in expected or already_used or not isinstance(saved, dict)
                or (marker.get("kind") != "file" and (not visible or previous is not visible[-1]))):
            raise HTTPException(409, {"code": "turn_unavailable", "message": "這一輪已結束或無法繼續。"})
        self.previous = previous
        self.kind = marker["kind"]
        self.task_id = marker.get("task_id")
        self.has_checkpoint = marker.get("checkpoint", False)
        self.supplement = self.request.question.strip() if self.request.clarification_reply_to else ""
        self.supplements = list(previous.get("_web_supplements", []))
        if self.supplement:
            self.supplements.append(self.supplement)
        if self.request.file_ids and self.supplement:
            # Reading a new set of inputs requires a fresh document run.
            if set(self.request.file_ids) != set(saved["file_ids"]):
                self.replaced_task_id, self.task_id = self.task_id, None
                self.has_checkpoint = False
                saved = {**saved, "file_ids": list(self.request.file_ids), "file_references": self.request.file_references}
        self.request = self.request.model_copy(update=saved)
        object.__setattr__(self.request, "_web_turn", self)
        object.__setattr__(self.request, "_channel_conversation_id", self.cid)
        object.__setattr__(self.request, "_channel_lock_held", True)

    async def save_user(self):
        if self.previous and not self.supplement:
            return self.previous["_web_user_message"]
        message = {
            "id": self.user_id, "content": self.supplement or self.request.question.strip(),
            "is_user": True, "timestamp": datetime.now().isoformat(),
            "file_ids": list(self.request.file_ids if self.resolved_files is None else self.resolved_files),
            "file_references": self.request.file_references,
            **({"photo_files": self.photo_files} if getattr(self, "photo_files", None) else {}),
            **({"attachment_files": self.attachment_files} if getattr(self, "attachment_files", None) else {}),
        }
        await self.host.data_store.append_owned_messages(self.owner, self.cid, [message])
        return self.user_id

    @property
    def context(self):
        if not self.previous:
            return ""
        supplement = "\n\n使用者補充資料：\n" + "\n".join(self.supplements) if self.supplements else ""
        if self.supplement:
            return supplement
        if self.previous.get("content") and self.kind == "chat":
            visible = re.sub(r"<think>.*?</think>", "", self.previous["content"], flags=re.S)
            return supplement + (
                "\n\n前次回答被使用者停止。請從已顯示內容接續，避免重複，使用本輪已驗證的參考資料。"
                "\n已顯示內容：\n" + visible
            )
        return supplement

    def parent_metadata(self):
        return {"_web_parent": self.previous["id"]} if self.previous else {}

    async def publish_pause(self):
        status = ("waiting" if self.question and not self.action else
                  "cancelled" if self.action == "cancel" else "paused" if self.kind == "file" else "stopped")
        text = self.text if self.visible_length is None else self.text[:self.visible_length]
        if self.kind == "chat":
            # The browser may also receive provider reasoning in a collapsed panel.
            # Keep whole reasoning blocks and close any current block before replay.
            if text.rfind("<think>") > text.rfind("</think>"):
                text += "</think>"
        marker = {"id": self.reply_id, "status": status, "kind": self.kind,
                  **({"question": self.question} if status == "waiting" else {}),
                  **({"task_id": self.task_id, "checkpoint": self.has_checkpoint} if self.kind == "file" else {})}
        user_message = await self.save_user()
        record = {
            "id": self.reply_id, "content": text, "is_user": False,
            "timestamp": datetime.now().isoformat(), "turn_control": marker,
            "answer_status": "interrupted", **self.metadata,
            "_web_request": self.request.model_dump(include={
                "question", "think", "search", "trace", "file_references", "file_ids", "skill_id", "model"
            }),
            "_web_user_message": user_message, "_web_supplements": self.supplements, **self.parent_metadata(),
            "_web_plan": self.plan,
        }
        if self.resolved_files is not None:
            record["_web_request"]["file_ids"] = self.resolved_files
        if record.get("web_status", {}).get("status") in {"processing", "searching", "fetching"}:
            record["web_status"] = {"status": "partial" if record.get("web_sources") else "error",
                                    "message": "網頁搜尋已停止。"}
        trace = self.process_tracker.finalize(status)
        if trace["steps"]:
            record["process_trace"] = trace
            self.metadata["process_trace"] = trace
        await self.host.data_store.append_owned_messages(self.owner, self.cid, [record])
        return event({"type": "turn_control", **marker, "text": text, "conversation_id": self.cid,
                      **{key: record[key] for key in self.metadata}})

    def capture(self, data):
        kind = data.get("type")
        if kind in {"done", "error"}:
            self.finished = True
            self.completed = kind == "done"
        elif kind == "content":
            self.text += data.get("text", "")
        elif kind == "process_step":
            self.process_tracker.ingest(data)
            if isinstance(data.get("config"), dict):
                self.process_tracker.update_config(**data["config"])
        elif kind == "model_info":
            self.process_tracker.update_config(
                model_id=data.get("requested_model"),
                model_name=data.get("model_name"),
            )
        elif kind == "skill_context":
            self.metadata["skill_context"] = {k: v for k, v in data.items() if k != "type"}
        elif kind == "rag_context":
            self.metadata["citation_map"] = data.get("chunks", [])
            self.metadata["rag_status"] = {k: v for k, v in data.items() if k not in {"type", "chunks"}}
        elif kind in {"web_status", "web_sources"}:
            self.metadata["web_status"] = {"status": data.get("status"), "message": data.get("message")}
            if kind == "web_sources":
                self.metadata["web_sources"] = data.get("sources", [])


class BrowserTurnResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.body_iterator.aclose()


def browser_turn_response(host, scheduler, handler, request, session_id, owner, cid):
    turn = BrowserTurn(host, request, session_id, owner, cid)
    key = (id(host.data_store), turn.id)
    if key in active_turns:
        raise HTTPException(409, "此回合正在執行。")

    async def stream():
        if key in active_turns:
            yield event({"type": "error", "detail": "此回合正在執行。"})
            return
        active_turns[key] = turn
        queue = asyncio.Queue(16)
        connected = True

        async def emit(item):
            if connected:
                await queue.put(item)

        async def run_handler():
            response = await handler(turn.request, session_id)
            if not isinstance(response, StreamingResponse):
                raise HTTPException(500, "回覆格式有誤。")

            async def receive():
                await asyncio.Future()

            async def send(message):
                body = message.get("body")
                if not body:
                    return
                text = body.decode("utf-8") if isinstance(body, bytes) else body
                for line in text.splitlines():
                    if line.startswith("data: {"):
                        turn.capture(json.loads(line[6:]))
                await emit(text)

            try:
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
            finally:
                close = getattr(response.body_iterator, "aclose", None)
                if close:
                    await close()

        async def drive():
            locked = False
            try:
                await scheduler.acquire(host.data_store, owner, cid, turn.id)
                locked = True
                object.__setattr__(turn.request, "_channel_conversation_id", cid)
                object.__setattr__(turn.request, "_channel_lock_held", True)
                object.__setattr__(turn.request, "_web_turn", turn)
                await turn.prepare()
                if not turn.action:
                    await emit(event({"type": "status", "status": "planning", "message": "正在理解你的問題…"}))
                    await emit(turn.process_tracker.sse(
                        "plan", "plan", "running", "正在理解問題並規劃處理方式…",
                    ))
                if not turn.action:
                    turn.worker = asyncio.create_task(run_handler())
                    try:
                        await turn.worker
                    except asyncio.CancelledError:
                        if not turn.action:
                            raise
                if turn.action or turn.question:
                    terminal = await turn.publish_pause()
                    turn.finished = True
                    await emit(terminal)
                if turn.replaced_task_id and (turn.completed or turn.action or turn.question):
                    await discard_file_draft(host, owner, turn.replaced_task_id)
            except HTTPException as error:
                if isinstance(error.detail, dict) and error.detail.get("code") == "turn_unavailable":
                    await emit(event({"type": "error", "code": error.detail["code"], "detail": error.detail["message"]}))
                else:
                    await emit(event({"type": "error", "detail": str(error.detail)}))
            except Exception:
                logging.exception("Interactive browser turn failed")
                await emit(event({"type": "error", "detail": "無法保存或完成本輪，請重試。"}))
            finally:
                if locked:
                    scheduler.release(host.data_store, owner, cid, turn.id)
                active_turns.pop(key, None)
                await emit(None)

        turn.work = asyncio.create_task(drive())
        try:
            yield event({"type": "turn_started", "turn_id": turn.id, "conversation_id": cid,
                         "user_message_id": turn.user_id, "reply_id": turn.reply_id})
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            connected = False
            # The response is a subscriber. Only the control endpoint can pause
            # its worker; draining releases a producer blocked on the socket.
            while not queue.empty():
                queue.get_nowait()

    return BrowserTurnResponse(stream(), media_type="text/event-stream",
                               headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def control_turn(host, session_id, turn_id, action, visible_length, conversation_id):
    session = await host.data_store.get_session(session_id)
    if not session:
        raise HTTPException(401, "會話無效。")
    turn = active_turns.get((id(host.data_store), turn_id))
    if turn and turn.session_id == session_id and turn.owner == session["account"] and turn.cid == conversation_id:
        if action == "cancel" and turn.kind != "file":
            raise HTTPException(409, "本輪沒有可取消的文件任務。")
        return {"status": turn.stop(action, visible_length)}
    conversation = await host.data_store.get_conversation(conversation_id)
    if not conversation or conversation["user_id"] != session["account"]:
        raise HTTPException(404, "找不到這一輪執行。")
    if action == "cancel":
        return await cancel_saved_file(host, session["account"], conversation_id, turn_id)
    return {"status": "finished"}


async def discard_file_draft(host, owner, task_id):
    if task_id:
        from services.file_agent.runtime import TaskRuntime

        try:
            await asyncio.to_thread(TaskRuntime.discard_checkpoint, Path(host.UPLOAD_DIR), owner, task_id)
        except (OSError, ValueError):
            logging.warning("Unable to remove the stopped file task's temporary files", exc_info=True)


async def cancel_saved_file(host, owner, cid, message_id):
    from services.channel_chat import scheduler

    lane = str(uuid4())
    await scheduler.acquire(host.data_store, owner, cid, lane)
    try:
        convo = await host.data_store.get_conversation(cid)
        messages = convo.get("messages", []) if convo and convo["user_id"] == owner else []
        previous = next((item for item in messages if item.get("id") == message_id), {})
        marker = previous.get("turn_control", {})
        if marker.get("kind") != "file" or marker.get("status") not in PAUSED:
            raise HTTPException(404, "找不到可取消的文件任務。")
        existing = next((item for item in messages if item.get("_web_parent") == message_id), None)
        if existing:
            if existing.get("turn_control", {}).get("status") == "cancelled":
                await discard_file_draft(host, owner, marker.get("task_id"))
                return {"status": "cancelled"}
            raise HTTPException(409, "此任務已繼續或完成。")
        identifier = str(uuid4())
        await host.data_store.append_owned_messages(owner, cid, [{
            "id": identifier, "is_user": False, "content": "", "timestamp": datetime.now().isoformat(),
            "turn_control": {"id": identifier, "status": "cancelled", "kind": "file"},
            "answer_status": "interrupted", "_web_parent": message_id,
        }])
        await discard_file_draft(host, owner, marker.get("task_id"))
        return {"status": "cancelled"}
    finally:
        scheduler.release(host.data_store, owner, cid, lane)
