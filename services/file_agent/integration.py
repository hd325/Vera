"""Reuse existing authenticated conversations, file records, preview and downloads."""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import datetime
from pathlib import Path

from services.resource_limits import PROCESSING_SLOT
from config.settings import ENABLE_FILE_AGENT
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from services.chat_learning import valid_chat_messages
from services.original_files import original_artifacts
from services.process_trace import ProcessTracker, file_step_links

from .agent import DOCUMENT_EXPORT_FORMATS, LLMFileAgent, export_text_content, word_content
from .runtime import FileAgentError, TaskRuntime

_locks: dict[tuple[str, str], asyncio.Lock] = {}
_active_tasks = 0


def _event(value):
    return "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"


async def export_answer_text(runtime, previous_text, formats, authorize):
    outputs = []
    for fmt in formats:
        await authorize()
        content = export_text_content(previous_text, fmt)
        result = await runtime.call("create_file", {"format": fmt, "filename": f"對話內容.{fmt}", "content": content})
        outputs.append(result["file_id"])
    return outputs


async def read_input_pages(runtime, authorize):
    """Load small inputs fully and hand off explicit page offsets for larger inputs."""
    sources = []
    remaining = 30000
    for file_id, record in runtime.records.items():
        offset = 0
        items = []
        source = {"file_id": file_id, "filename": record.get("original_name") or record.get("name"),
                  "items": items, "complete": False, "next_start": 0}
        sources.append(source)
        while remaining > 0:
            await authorize()
            limit = 40
            while limit:
                try:
                    page = await runtime.call("read_file", {"file_id": file_id, "start": offset, "limit": limit})
                    break
                except FileAgentError:
                    limit //= 2
            if not limit:
                break
            batch = page.get("items", [])
            items.extend(batch)
            source["format"] = page.get("format")
            if page.get("uploaded_extracted_text"):
                source.setdefault("recognized_text", []).append(page["uploaded_extracted_text"])
            remaining -= len(json.dumps(batch, ensure_ascii=False)) + len(page.get("uploaded_extracted_text", ""))
            offset = page.get("next_start")
            source["next_start"] = offset
            source["complete"] = not page.get("has_more")
            if source["complete"] or not isinstance(offset, int):
                break
    return sources


def conversion_source(sources):
    """Use one complete text source, or an equivalent Markdown/Word pair."""
    if not sources or any(not item["complete"] for item in sources):
        return ""
    markdowns, word_docs = [], []
    for source in sources:
        if source.get("format") in {"md", "markdown", "txt"}:
            markdowns.append("\n".join(item["text"] for item in source["items"]))
        elif source.get("format") == "docx" and all(item.get("type") == "paragraph" for item in source["items"]):
            word_docs.append([item for item in source["items"] if item["text"].strip()])
        else:
            return ""
    if len(markdowns) == 1:
        if not word_docs:
            return markdowns[0]
        if len(word_docs) == 1 and (
            [item["text"].strip() for item in word_content(markdowns[0])["paragraphs"]]
            == [item["text"].strip() for item in word_docs[0]]
        ):
            return markdowns[0]
    if not markdowns and len(word_docs) == 1:
        return "\n\n".join(
            "#" * int(item["style"][-1]) + " " + item["text"]
            if item["style"] in {f"Heading {level}" for level in range(1, 7)} else item["text"]
            for item in word_docs[0]
        )
    return ""


async def maybe_file_response(host, request, session_id, *, plan, chosen, previous_text=""):
    """Execute a file action already decided by the model."""
    if not ENABLE_FILE_AGENT:
        raise HTTPException(503, "文件工具尚未啟用。")
    turn = getattr(request, "_web_turn", None)
    session = await host.data_store.get_session(session_id)
    if not session:
        raise HTTPException(401, "會話無效")
    user_id = session["account"]
    current_id = getattr(request, "_channel_conversation_id", None) or session.get("current_conversation_id")
    conversation = await host.data_store.get_conversation(current_id) if current_id else None
    history = valid_chat_messages(conversation.get("messages", [])) if conversation and conversation.get("user_id") == user_id else []
    original = None
    if plan["action"] == "original":
        artifacts = await original_artifacts(host, user_id, chosen, formats=plan["formats"])
        if not artifacts:
            raise HTTPException(422, "目前找不到符合要求的原始檔案。")
        original = {"text": "已找到原始檔案：" + "、".join(item["name"] for item in artifacts) + "。",
                    "referenced_files": [item["id"] for item in artifacts],
                    "remaining_question": plan["remaining_question"]}
    if original and original["remaining_question"]:
        if turn:
            turn.kind = "chat"
        object.__setattr__(request, "_original_delivery", original)
        return None
    if turn:
        turn.kind = "file"
    if conversation and conversation.get("user_id") != user_id:
        raise HTTPException(403, "無權訪問此對話")
    if not conversation:
        response = await host.new_conversation(host.NewConversationRequest(session_id=session_id), session_id)
        current_id = response["data"]["conversation_id"]
        conversation = await host.data_store.get_conversation(current_id)
    if original is not None:
        async def deliver_original():
            tracker = ProcessTracker(
                started_at=turn.process_tracker.started_at if turn else None,
                config={"mode": "file", "action": "original"},
            )
            yield tracker.sse("plan", "plan", "done", "已理解問題", detail="定位原始檔案")
            yield tracker.sse("file_original", "file", "running", "正在取得原始文件…")
            yield _event({"type": "file_status", "status": "reading", "message": "正在取得原始文件"})
            current_session = await host.data_store.get_session(session_id)
            if not current_session or current_session.get("account") != user_id:
                yield _event({"type": "error", "detail": "登入已失效，請重新登入。"})
                return
            try:
                artifacts = await original_artifacts(host, user_id, original["referenced_files"])
                stamp = datetime.now().isoformat()
                reply_id = turn.reply_id if turn else str(uuid.uuid4())
                yield tracker.sse(
                    "file_original", "file", "done",
                    f"已找到 {len(artifacts)} 份原始文件" if artifacts else "已完成原檔檢查",
                    links=file_step_links(artifacts, sublabel="原檔"),
                )
                process_trace = tracker.finalize("done")
                messages = [{"id": str(uuid.uuid4()), "content": request.question, "is_user": True,
                             "timestamp": stamp, "file_ids": list(request.file_ids),
                             "file_references": request.file_references},
                            {"id": reply_id, "content": original["text"], "is_user": False,
                             "timestamp": stamp, "artifacts": artifacts,
                             "referenced_files": original["referenced_files"], "file_status": "complete",
                             "original_file_delivery": True, "process_trace": process_trace,
                             **(turn.parent_metadata() if turn else {})}]
                if turn:
                    turn.begin_commit()
                    await turn.save_user()
                    messages = messages[1:]
                await host.data_store.append_owned_messages(user_id, current_id, messages)
                if artifacts:
                    yield _event({"type": "artifacts", "artifacts": artifacts})
                yield _event({"type": "content", "text": original["text"]})
                yield _event({"type": "file_status", "status": "complete", "message": "原文件已就緒" if artifacts else "請確認文件"})
                yield _event({"type": "done", "artifacts": artifacts, "file_status": "complete",
                              "citation_map": [], "references": [], "message_id": reply_id,
                              "conversation_id": current_id, "process_trace": process_trace})
            except HTTPException as exc:
                yield _event({"type": "error", "detail": exc.detail})
        return StreamingResponse(deliver_original(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    lock_key = (user_id, current_id)
    lock = _locks.setdefault(lock_key, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "本對話有文件任務正在執行，請完成後再修改。")
    global _active_tasks
    if _active_tasks >= 1 or PROCESSING_SLOT.locked():
        raise HTTPException(429, "文件任務繁忙，請等待現有任務完成。")

    records = await host.data_store.get_all_files(user_id)
    by_id = {record["id"]: record for record in records}
    chosen = list(dict.fromkeys(chosen))
    for file_id in chosen:
        record = by_id.get(file_id) or await host._authorized_turn_file(file_id, user_id)
        if not record:
            raise HTTPException(403, "引用檔案不存在或沒有使用權限")
        by_id[file_id] = record
    if not chosen and (plan["source"] == "attached" or plan["action"] == "edit"):
        detail = "請指定要處理的原始檔案。"
        if turn:
            turn.clarify(detail)
            return StreamingResponse(iter(()), media_type="text/event-stream")
        raise HTTPException(422, detail)
    if len(chosen) > 12:
        raise HTTPException(400, "每次最多引用 12 份檔案。")
    if turn:
        turn.resolved_files = chosen
    # Recheck after all awaited lookups, then reserve without yielding.
    lock = _locks.setdefault(lock_key, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "本對話有文件任務正在執行，請完成後再修改。")
    if _active_tasks >= 1 or PROCESSING_SLOT.locked():
        raise HTTPException(429, "文件任務繁忙，請等待現有任務完成。")
    await lock.acquire()
    await PROCESSING_SLOT.acquire()
    _active_tasks += 1
    reservation_released = False

    def release_reservation():
        nonlocal reservation_released
        global _active_tasks
        if reservation_released:
            return
        reservation_released = True
        _active_tasks -= 1
        PROCESSING_SLOT.release()
        lock.release()
        if _locks.get(lock_key) is lock and not lock.locked():
            _locks.pop(lock_key, None)

    async def generate():
        runtime = None
        committed = False
        queue = asyncio.Queue()
        task = None
        artifacts = []
        reply_id = turn.reply_id if turn else str(uuid.uuid4())
        user_message_id = turn.user_id if turn else str(uuid.uuid4())
        history_before = copy.deepcopy(history)

        async def authorize():
            current_session = await host.data_store.get_session(session_id)
            current_conv = await host.data_store.get_conversation(current_id)
            if not current_session or current_session.get("account") != user_id:
                raise FileAgentError("登入已失效，文件任務已停止。")
            if not current_conv or current_conv.get("user_id") != user_id:
                raise FileAgentError("對話權限已失效，文件任務已停止。")
            for file_id in chosen:
                if not await host._authorized_turn_file(file_id, user_id):
                    raise FileAgentError("引用檔案權限已變更，文件任務已停止。")

        def checkpoint(progress):
            runtime.write_checkpoint(agent=progress)
            turn.has_checkpoint = True

        async def produce():
            try:
                resuming = bool(turn and turn.has_checkpoint)
                input_pages = []
                if runtime.records and not resuming:
                    await queue.put({"type": "file_status", "status": "reading", "message": "正在逐頁讀取原始文件"})
                    input_pages = await read_input_pages(runtime, authorize)
                source_text = previous_text or conversion_source(input_pages) if plan.get("export_previous") else ""
                if source_text and "xlsx" in plan["formats"] and not any(
                    sheet["name"] != "正文" for sheet in export_text_content(source_text, "xlsx")["sheets"]
                ):
                    # Prose must be structured into cells by the file agent.
                    source_text = ""
                if (source_text and plan["formats"] and set(plan["formats"]) <= DOCUMENT_EXPORT_FORMATS
                        and not resuming):
                    await queue.put({"type": "file_status", "status": "generating", "message": "正在匯出上一輪的正文"})
                    output_ids = await export_answer_text(runtime, source_text, plan["formats"], authorize)
                    await queue.put({"type": "result", "output_ids": output_ids, "summary": "已匯出對話內容。"})
                    return
                agent = LLMFileAgent(runtime, host.API_CONFIG, authorize)
                agent.memory_context = memory_text
                agent.model = getattr(request, "_channel_model", None) or request.model
                if not resuming:
                    from services.photo_pipeline import referenced_photos, visual_evidence
                    photos = await referenced_photos(host, user_id, chosen)
                    if photos:
                        await queue.put({"type": "file_status", "status": "reading", "message": "正在查看來源照片"})
                        await authorize()
                        try:
                            async with asyncio.timeout(24):
                                evidence = await visual_evidence(host, photos, request.question)
                        except (OSError, ValueError, TimeoutError):
                            evidence = ""
                        if evidence:
                            agent.memory_context += "\n【本輪照片畫面讀取結果】\n" + evidence[:18000]
                        else:
                            agent.memory_context += "\n【照片】畫面暫時無法讀取。只使用可用的 OCR 資料，並說明此限制。"
                options = {"checkpoint": checkpoint, "supplements": turn.supplements} if turn else {}
                async for item in agent.run(request.question, history_before, previous_text=previous_text,
                                            formats=plan["formats"], practice_questions=plan.get("practice_questions"),
                                            input_pages=input_pages, action=plan["action"],
                                            workbook=plan.get("workbook"),
                                            **options):
                    await queue.put(item)
            except BaseException as exc:
                await queue.put(exc)
            finally:
                await queue.put(None)

        tracker = ProcessTracker(
            started_at=turn.process_tracker.started_at if turn else None,
            config={
                "mode": "file",
                "action": plan["action"],
                "formats": list(plan.get("formats") or []),
                "model_id": getattr(request, "_channel_model", None) or request.model,
            },
        )
        active_file_step = None
        step_seq = 0
        try:
            memory_text, memory_refs = await host.memory_context(host, user_id, request.question)
            fmt_label = "、".join(fmt.upper() for fmt in (plan.get("formats") or ()))
            plan_detail = f"檔案任務 · {fmt_label}" if fmt_label else "檔案處理任務"
            yield tracker.sse(
                "plan", "plan", "done", "已理解問題",
                detail=plan_detail,
                note=plan.get("progress_note") or "",
            )
            if turn and turn.has_checkpoint:
                try:
                    runtime = TaskRuntime.restore_checkpoint(Path(host.UPLOAD_DIR), user_id, current_id, turn.task_id)
                    if set(runtime.records) != set(chosen) or runtime.browser_progress is None:
                        raise FileAgentError("檢查點與本輪引用不一致。")
                    runtime.prune_untracked_outputs()
                except FileAgentError:
                    runtime = None
                    turn.replaced_task_id, turn.task_id = turn.task_id, None
                    turn.has_checkpoint = False
                    yield _event({"type": "file_status", "status": "reading", "message": "檢查點無法使用，將從頭開始。"})
            restored = runtime is not None
            if not restored:
                runtime = TaskRuntime(Path(host.UPLOAD_DIR), user_id, current_id)
            if turn:
                turn.task_id = runtime.task_id
            runtime.write_state("running")
            source_links = file_step_links([by_id[fid] for fid in chosen if fid in by_id], sublabel="來源檔案")
            active_file_step = ("file_prepare", "file", "讀取引用資料")
            yield tracker.sse(
                "file_prepare", "file", "running",
                f"正在準備 {len(chosen)} 份引用資料…" if chosen else "正在初始化文件工作區…",
                links=source_links if source_links else None,
            )
            yield _event({"type": "file_status", "task_id": runtime.task_id,
                          "status": "reading", "message": "正在讀取引用資料"})
            await authorize()
            if not restored:
                for file_id in chosen:
                    record = by_id[file_id]
                    source = host.resolve_storage_path(record.get("storage_path"), record)
                    if not source or not Path(source).is_file():
                        raise FileAgentError("引用檔案原始文件不存在，請重新上傳後再處理。")
                    text = await host.data_store.get_file_content(file_id) or ""
                    copy_input = asyncio.create_task(asyncio.to_thread(runtime.add_input, record, Path(source), text))
                    try:
                        await asyncio.shield(copy_input)
                    except asyncio.CancelledError:
                        await asyncio.gather(copy_input, return_exceptions=True)
                        raise
            yield tracker.sse(
                "file_prepare", "file", "done",
                f"已載入 {len(chosen)} 份引用檔案" if chosen else "已初始化文件工作區",
                links=source_links if source_links else None,
            )
            active_file_step = None
            if turn:
                user_message_id = await turn.save_user()
            else:
                conversation["messages"].append({"id": user_message_id, "content": request.question,
                    "is_user": True, "timestamp": datetime.now().isoformat(),
                    "file_ids": chosen, "file_references": request.file_references})
                conversation["updated_at"] = datetime.now().isoformat()
                await host.data_store.set_conversation(current_id, conversation)
            task = asyncio.create_task(produce())
            summary = ""
            output_ids = []
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), 15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if isinstance(item, BaseException):
                    raise item
                if item is None:
                    break
                if item["type"] == "result":
                    summary = item["summary"]
                    output_ids = item["output_ids"]
                elif item["type"] == "clarification" and turn:
                    turn.clarify(item["question"])
                    runtime.write_state("waiting")
                    return
                else:
                    if item.get("type") == "file_status":
                        if active_file_step:
                            prev_id, prev_kind, prev_done_title = active_file_step
                            yield tracker.sse(prev_id, prev_kind, "done", prev_done_title)
                            active_file_step = None
                        step_seq += 1
                        fstatus = str(item.get("status") or "working")
                        fmsg = str(item.get("message") or "正在處理檔案")
                        fname = str(item.get("filename") or "")
                        if item.get("progress_note"):
                            yield tracker.sse(
                                f"file_note_{step_seq}", "note", "done", "任務說明",
                                note=str(item["progress_note"]),
                            )
                        else:
                            step_kind = "edit" if fstatus in {"editing", "generating"} else "validate" if fstatus in {"validating", "checking"} else "file"
                            done_title = ("已" + fmsg[2:].rstrip("….")) if fmsg.startswith("正在") else f"已完成：{fmsg}"
                            sid = f"file_step_{step_seq}"
                            active_file_step = (sid, step_kind, done_title + (f" · {fname}" if fname else ""))
                            yield tracker.sse(
                                sid, step_kind, "running",
                                fmsg + (f"（{fname}）" if fname else "") + "…",
                            )
                    yield _event({**item, "task_id": runtime.task_id})
            if active_file_step:
                prev_id, prev_kind, prev_done_title = active_file_step
                yield tracker.sse(prev_id, prev_kind, "done", prev_done_title)
                active_file_step = None
            if not runtime.generated:
                raise FileAgentError("本次任務未產生可下載的檔案。")
            await authorize()
            yield tracker.sse("file_validate", "validate", "running", "正在檢查並保存完整文件…")
            yield _event({"type": "file_status", "task_id": runtime.task_id,
                          "status": "validating", "message": "正在檢查並保存完整文件"})
            # Keep final leaves; internal edit iterations do not become user downloads.
            final = {key: runtime.generated[key] for key in output_ids}
            records_to_save = []
            for file_id, value in final.items():
                path = runtime.paths[file_id]
                validation = await runtime.call("validate_file", {"file_id": file_id})
                parent_id = value.get("parent_id")
                while parent_id in runtime.generated:
                    parent_id = runtime.generated[parent_id].get("parent_id")
                parent = by_id.get(parent_id, {})
                root_id = parent.get("root_id") or parent_id or file_id
                owner_versions = [int(record.get("version", 1)) for record in records
                                  if record.get("owner") == user_id and (record.get("root_id") or record["id"]) == root_id]
                version = max(owner_versions, default=int(parent.get("version", 0))) + 1 if parent_id else 1
                name = value.get("filename") or path.name
                if Path(name).name != name or "\\" in name:
                    raise FileAgentError("輸出檔案名稱不合法。")
                try:
                    text_result = await runtime.call("read_file", {"file_id": file_id, "limit": 5})
                except FileAgentError:
                    text_result = {"content": "文件已通過結構檢查。文字預覽無法完整載入，請下載原檔查看。", "truncated": True}
                # Searchable preview text; the original binary is always complete.
                extracted = json.dumps(text_result, ensure_ascii=False)
                now = datetime.now().isoformat()
                record = {"id": file_id, "name": name, "original_name": name,
                    "original_filename": name, "size": path.stat().st_size,
                    "uploaded_at": now, "owner": user_id, "audience": "personal",
                    "review_status": "published", "storage_path": path.relative_to(Path(host.UPLOAD_DIR).resolve()).as_posix(),
                    "file_task_id": runtime.task_id, "conversation_id": current_id,
                    "parent_id": parent_id, "root_id": root_id, "version": version,
                    "subject": "other", "material_type": "other", "study_period": "",
                    "notes": "AI 文件任務成果", "extraction_status": "partial" if text_result.get("has_more") or text_result.get("truncated") else "success",
                    "is_truncated": bool(text_result.get("has_more") or text_result.get("truncated")),
                    "page_count": validation.get("page_count", 0)}
                records_to_save.append((record, extracted))
                artifacts.append({"id": file_id, "name": name, "filename": name,
                    "size": record["size"], "file_size": record["size"], "has_original": True,
                    "version": version, "parent_id": parent_id,
                    "description": "已檢查並保存；可預覽及下載完整文件。"})
            await authorize()
            yield tracker.sse(
                "file_validate", "validate", "done",
                f"已驗證並保存 {len(artifacts)} 份文件",
                links=file_step_links(artifacts, sublabel="預覽檔案"),
            )
            process_trace = tracker.finalize("done")
            # All output metadata and the assistant message become durable together.
            # Use server-derived output names, not unverified model download claims.
            result_text = "已完成並檢查以下文件：" + "、".join(item["name"] for item in artifacts) + "。"
            message = {"id": reply_id, "content": result_text, "is_user": False,
                       "timestamp": datetime.now().isoformat(), "artifacts": artifacts,
                       "file_task_id": runtime.task_id, "file_status": "complete", "referenced_files": chosen,
                       "memory_refs": memory_refs, "process_trace": process_trace,
                       **(turn.parent_metadata() if turn else {}),
                       **(await host.pending_metadata(host, user_id, current_id, reply_id, request.model_copy(update={"skill_id": "none"}), user_message_id, request.question))}
            if turn:
                turn.begin_commit()
            await host.data_store.commit_file_task(current_id, user_id, records_to_save, message)
            committed = True
            for record, extracted in records_to_save:
                asyncio.create_task(host._index_fulltext_background(record, extracted))
            learning_meta = await host.finish_reply(host, user_id, current_id, reply_id)
            try:
                runtime.write_state("complete", output_ids=[item["id"] for item in artifacts])
            except OSError:
                pass  # The atomic file/message commit above is the durable authority.
            yield _event({"type": "artifacts", "task_id": runtime.task_id, "artifacts": artifacts})
            yield _event({"type": "content", "text": result_text})
            yield _event({"type": "file_status", "task_id": runtime.task_id, "status": "complete", "message": "文件已完成"})
            yield _event({"type": "done", "artifacts": artifacts, "file_task_id": runtime.task_id,
                          "file_status": "complete", "citation_map": [], "references": [],
                          "message_id": reply_id, "conversation_id": current_id,
                          "process_trace": process_trace, **learning_meta})
        except asyncio.CancelledError:
            if runtime and not committed:
                runtime.write_state("paused" if turn and turn.action == "pause" else "cancelled")
            raise
        except Exception as exc:
            detail = str(exc) if isinstance(exc, FileAgentError) else (
                "文件任務執行逾時，請拆分要求再試。" if isinstance(exc, TimeoutError)
                else "文件任務失敗，未保存不完整成果。請檢查原始文件或重試。")
            yield tracker.sse("file_error", "file", "error", "文件處理未完成", detail=detail)
            process_trace = tracker.finalize("error")
            if runtime and not committed:
                runtime.write_state("failed", error=detail)
            if not committed:
                current = await host.data_store.get_conversation(current_id)
                if current and current.get("user_id") == user_id:
                    current["messages"].append({"id": reply_id, "content": detail, "is_user": False,
                        "timestamp": datetime.now().isoformat(), "file_status": "error",
                        "file_task_id": runtime.task_id if runtime else None,
                        "referenced_files": chosen, "process_trace": process_trace,
                        **(turn.parent_metadata() if turn else {})})
                    await host.data_store.set_conversation(current_id, current)
            yield _event({"type": "file_status", "status": "error", "message": detail})
            yield _event({"type": "error", "detail": detail})
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            preserve = turn and turn.has_checkpoint and (turn.action == "pause" or turn.question) and not committed
            if runtime and not preserve:
                if turn and not committed:
                    turn.has_checkpoint = False
                try:
                    runtime.cleanup_temporary([item["id"] for item in artifacts] if committed else [])
                    (runtime.root / "checkpoint.json").unlink(missing_ok=True)
                except OSError:
                    pass
            release_reservation()

    class FileTaskResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                # Headers can fail before the generator's own finally is entered.
                release_reservation()

    return FileTaskResponse(generate(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
