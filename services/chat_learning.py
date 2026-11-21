"""Shared account-memory and inline-learning integration for both chat routes."""
from __future__ import annotations

import copy
import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import zhconv
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from services.teacher_pedagogy import chemistry_question, teaching_plan


def accounts(host):
    from services.account_service import get_controller
    return get_controller(host)


def chemistry_turn(request, question, recent_messages=()):
    if request.skill_id != "chemistry-teacher":
        return False
    q = zhconv.convert(question, "zh-cn").lower()
    if re.search(r"上传|发布|审核|登录|设置|校历|课程表|上课时间|生成.{0,6}(?:文件|ppt|word)|编辑文件", q):
        return False
    if chemistry_question(question):
        return True
    return teaching_plan(question)["route"] == "followup" and any(
        item.get("is_user") and chemistry_question(str(item.get("content", "")))
        for item in recent_messages[-6:]
    )


async def memory_context(host, owner, question):
    """Memory outages should not prevent an ordinary answer."""
    try:
        return await accounts(host).context(owner, question)
    except Exception:
        logging.warning("帳號記憶暫不可用，本次繼續回答。")
        return "", []


def web_query(question):
    """Send only a chemistry-topic query, with personal identifiers removed."""
    text = re.sub(r"\[[^\]\n]{1,180}\]", " ", question)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", " ", text)
    text = re.sub(r"(?:我叫|我是|我的名字是|我就读于|我就讀於)[^，,。；;？?\n]{1,60}[，,。；;]?", " ", text)
    text = re.sub(r"(?:高|初)[一二三123][\s\d一二三四五六七八九十-]{0,5}班", " ", text)
    text = re.sub(r"(?:我的?|我是|姓名|学籍号|學籍號|班级|班級|账号|賬號)[：:\s]*[\w\u4e00-\u9fff-]{1,20}[，,。；;：:]", " ", text)
    text = re.sub(r"\b\d{6,}\b|(?:sk-|Bearer\s+)[A-Za-z0-9_-]+", " ", text)
    return " ".join(text.split())[:256]


# 是否聯網、是否深度思考由 services/turn_planner.py 交給模型判斷，這裡不再有關鍵詞規則。


def _outbox(host, operation, owner, conversation_id, reply_id, payload=None):
    """Keep source snapshots in private work storage, outside chat messages."""
    data_root = Path(host.DATA_DIR).resolve()
    source_root = Path(__file__).resolve().parents[1]
    conversation_root = Path(getattr(host, "CONVERSATIONS_FILE", data_root / "conversations_data.json")).resolve().parent
    root = (conversation_root if data_root == source_root else data_root) / "storage"
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "chat_followups.sqlite3", timeout=15)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS followups (owner TEXT,conversation_id TEXT,reply_id TEXT,payload TEXT,PRIMARY KEY(owner,conversation_id,reply_id))")
        key = (owner, conversation_id, reply_id)
        if operation == "put":
            db.execute("INSERT OR IGNORE INTO followups VALUES(?,?,?,?)", (*key, json.dumps(payload, ensure_ascii=False)))
        elif operation == "get":
            row = db.execute("SELECT payload FROM followups WHERE owner=? AND conversation_id=? AND reply_id=?", key).fetchone()
            return json.loads(row[0]) if row else None
        elif operation == "delete":
            db.execute("DELETE FROM followups WHERE owner=? AND conversation_id=? AND reply_id=?", key)
        elif operation == "conversation":
            db.execute("DELETE FROM followups WHERE owner=? AND conversation_id=?", (owner, conversation_id))
        elif operation == "list":
            return db.execute("SELECT owner,conversation_id,reply_id FROM followups").fetchall()


async def pending_metadata(host, owner, conversation_id, reply_id, request, user_message_id, question,
                           citation_payload=(), web_context=None, skill_context=None):
    from services.account_service import personal_statement
    if request.skill_id != "chemistry-teacher" and not personal_statement(question):
        return {}
    payload = {
        "user_message_id": user_message_id, "question": question,
        "request": {"skill_id": request.skill_id, "search": request.search,
                    "file_ids": list(request.file_ids), "file_references": request.file_references},
        "citation_payload": list(citation_payload), "web_context": web_context,
        "skill_context": skill_context or {"skill_id": "none"},
    }
    await asyncio.to_thread(_outbox, host, "put", owner, conversation_id, reply_id, payload)
    return {"_learning_pending": {"reply_id": reply_id}}


async def finish_reply(host, owner, conversation_id, reply_id):
    conversation = await host.data_store.get_conversation(conversation_id)
    if not conversation or conversation.get("user_id") != owner:
        return {}
    message = next((m for m in conversation.get("messages", []) if m.get("id") == reply_id), None)
    if not message:
        return {}
    if not message.get("_learning_pending"):
        return {k: message[k] for k in ("interactions", "memory_refs", "memory_update") if k in message}
    try:
        marker = await asyncio.to_thread(_outbox, host, "get", owner, conversation_id, reply_id)
        if not marker:
            return {}
        memory_update = await accounts(host).after_reply(owner, conversation_id, marker["user_message_id"], reply_id, marker["question"])
        from services.chat_tutor import after_reply
        request = SimpleNamespace(**marker["request"])
        extra = await after_reply(host, request, owner, conversation_id, reply_id,
                                 marker["question"], message.get("content", ""),
                                 marker.get("citation_payload", []), marker.get("web_context"),
                                 marker.get("skill_context", {}))
        current = await host.data_store.get_conversation(conversation_id)
        if not current or current.get("user_id") != owner:
            return {}
        target = next((m for m in current.get("messages", []) if m.get("id") == reply_id), None)
        if not target:
            return {}
        target.update(extra or {})
        if memory_update:
            target["memory_update"] = memory_update
        target.pop("_learning_pending", None)
        await host.data_store.set_conversation(conversation_id, current)
        await asyncio.to_thread(_outbox, host, "delete", owner, conversation_id, reply_id)
        return {k: target[k] for k in ("interactions", "memory_refs", "memory_update") if k in target}
    except Exception:
        # A saved marker permits restart recovery; the completed answer survives.
        logging.warning("學習後續工作暫不可用，已保留回答與待處理標記。")
        return {"memory_refs": message.get("memory_refs", [])}


async def recover_outbox(host):
    for conversation in list(getattr(host.data_store, "_conversations", {}).values()):
        for message in list(conversation.get("messages", [])):
            if message.get("_learning_pending") and not message.get("is_user"):
                await finish_reply(host, conversation["user_id"], conversation["id"], message["id"])
    # A crash before saving a reply, or after removing its marker, leaves no work
    # to replay. Drop such snapshots rather than retaining unused source bodies.
    for owner, conversation_id, reply_id in await asyncio.to_thread(_outbox, host, "list", "", "", ""):
        conversation = await host.data_store.get_conversation(conversation_id)
        live = conversation and conversation.get("user_id") == owner and any(
            m.get("id") == reply_id and m.get("_learning_pending") for m in conversation.get("messages", []))
        if not live:
            await asyncio.to_thread(_outbox, host, "delete", owner, conversation_id, reply_id)


async def cancel_outbox(host, owner, conversation_id):
    await asyncio.to_thread(_outbox, host, "conversation", owner, conversation_id, "")


def valid_chat_messages(messages):
    """Exclude verified replay errors from projections, retaining the audit store."""
    return [message for message in messages
            if message.get("is_user") or message.get("channel_replay_invalidated") is not True]


def public_messages(messages):
    visible = valid_chat_messages(messages)
    children = {message["_web_parent"]: message for message in visible if message.get("_web_parent")}
    last = visible[-1] if visible else None
    result = []
    for message in visible:
        if message.get("_web_parent") and message.get("turn_control", {}).get("status") == "cancelled":
            continue
        value = {k: copy.deepcopy(v) for k, v in message.items() if not k.startswith("_")}
        if value.get("file_status") == "error" and str(value.get("content", "")).lstrip().startswith("<|tool_call_start|>"):
            value["content"] = "這一輪的文件未生成。你可以在同一對話重新提出要求。"
        control = value.get("turn_control")
        if control:
            successor = children.get(message.get("id"))
            if successor and successor.get("turn_control", {}).get("status") == "cancelled":
                control.update(status="cancelled", closed=True)
                control.pop("question", None)
                control.pop("checkpoint", None)
            elif successor or (control["kind"] == "chat" and message is not last):
                control["closed"] = True
        if "memory_refs" in value:
            value["memory_refs"] = [{k: str(item[k]) for k in ("id", "content", "category") if k in item}
                                    for item in value["memory_refs"] if isinstance(item, dict)]
        result.append(value)
    return result


async def memory_command_response(host, request, session_id):
    session = await host.data_store.get_session(session_id)
    if not session:
        raise HTTPException(401, "會話無效")
    owner = session["account"]
    current_id = getattr(request, "_channel_conversation_id", None) or session.get("current_conversation_id")
    conversation = await host.data_store.get_conversation(current_id) if current_id else None
    if conversation and conversation.get("user_id") != owner:
        # Reset an invalid stale pointer; never append to someone else's chat.
        conversation = None
    command = await accounts(host).command(owner, request.question, valid_chat_messages(conversation.get("messages", [])) if conversation else [])
    if not command or not command.get("handled"):
        return None
    if conversation is None:
        response = await host.new_conversation(host.NewConversationRequest(session_id=session_id), session_id)
        current_id = response["data"]["conversation_id"]
        conversation = await host.data_store.get_conversation(current_id)
    now = datetime.now().isoformat()
    reply_id = str(uuid4())
    text = command["content"]
    refs = command.get("memory_refs", [])
    conversation["messages"].extend([
        {"id": str(uuid4()), "content": request.question, "is_user": True, "timestamp": now},
        {"id": reply_id, "content": text, "is_user": False, "timestamp": now,
         "memory_refs": refs, "skill_id": "none", "skill_context": {"skill_id": "none"}},
    ])
    if len(conversation["messages"]) == 2:
        conversation["title"] = request.question[:30]
    conversation["updated_at"] = now
    await host.data_store.set_conversation(current_id, conversation)
    async def generate():
        for event in ({"type": "content", "text": text},
                      {"type": "done", "message_id": reply_id, "conversation_id": current_id,
                       "memory_refs": refs, "interactions": [], "citation_map": [],
                       "skill_id": "none", "skill_context": {"skill_id": "none"}}):
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
