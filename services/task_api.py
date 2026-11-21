"""Authenticated persistent work, practice attempts and PDF exports."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import io
import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from uuid import uuid4

from fastapi import Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from services.review_policy import is_published_record
from services.school_quiz import generate_school_quiz, grade_answer, normal
from services.quiz_service import QuizGenerationError, QuizValidationError, validate_options
from services.task_store import LearningDataCleared, TaskStore, now
from utils.image_utils import PHOTO_EXTENSIONS


managed_upload = contextvars.ContextVar("managed_upload", default=None)
ACTIVE = {"queued", "running"}
DRAFT_FIELDS = {"audience", "subject", "subject_custom", "study_period", "material_type", "material_custom", "notes", "target_class"}


def fingerprint(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def public_task(task, detail=False):
    value = {k: v for k, v in task.items() if k not in {"owner", "payload", "extraction", "partial", "result", "draft_clients", "classification_evidence", "classification_defaults"}}
    if detail:
        value["classification_evidence"] = task.get("classification_evidence", {})
        value["classification_defaults"] = task.get("classification_defaults", {})
        value["result"] = task.get("result")
        if value["result"] and task["kind"] == "quiz":
            value["result"] = {**value["result"], "questions": [
                {k: v for k, v in q.items() if k not in {"answer", "explanation", "rubric", "accepted_answers", "decimals", "original_text"}}
                for q in value["result"]["questions"]]}
            if task.get("origin") == "chat":
                allowed = {"type", "stem", "options", "topic", "source_kind", "source_label", "document_id", "page", "chunk_id", "filename", "url", "title", "captured_at", "verification", "origin"}
                value["result"]["questions"] = [{k: v for k, v in q.items() if k in allowed}
                                                  for q in value["result"]["questions"]]
    return value


class TaskController:
    def __init__(self, host, root=None):
        self.host = host
        self.store = TaskStore(root or host.DATA_DIR)
        self.store.recover()
        self.store.expire_drafts()
        self.last_cleanup = time.monotonic()
        self.workers = {}
        self.locks = {}
        self.upload_slots = asyncio.Semaphore(2)
        self.quiz_slots = asyncio.Semaphore(1)

    async def db(self, method, *args, **kwargs):
        try:
            return await asyncio.to_thread(getattr(self.store, method), *args, **kwargs)
        except LearningDataCleared as exc:
            raise HTTPException(409, str(exc)) from exc

    async def upload_detail(self, task):
        value = public_task(task, True)
        if task["kind"] == "upload":
            draft = task.get("draft", {})
            if task.get("committed_file_id"):
                record = await self.host.data_store.find_file_by_id(task["committed_file_id"])
                value["filename_preview"] = record.get("name", task["name"]) if record else task["name"]
            else:
                grade, class_name = self.host.audience_name_scope(
                    task["owner"], draft.get("audience", ""), draft.get("target_class", ""))
                value["filename_preview"] = self.host.generate_file_name(
                    task["name"], draft.get("audience", ""), draft.get("subject", ""),
                    draft.get("material_type", ""), draft.get("material_custom", ""),
                    draft.get("study_period", ""), draft.get("notes", ""),
                    task["owner"], self.host.data_store, subject_custom=draft.get("subject_custom", ""),
                    audience_grade=grade, audience_class=class_name,
                )
        return value

    def lock(self, key):
        return self.locks.setdefault(key, asyncio.Lock())

    def directory(self, task_id):
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise HTTPException(404, "未找到任務")
        return self.store.root / task_id

    async def owned(self, task_id, session_id):
        session = await self.host.data_store.get_session(session_id)
        task = await self.db("get", task_id, session["account"] if session else "")
        if not task:
            raise HTTPException(404, "未找到任務")
        return task

    async def document(self, document_id, owner):
        h = self.host
        if h.is_deleted_file_record({"document_id": document_id}):
            raise HTTPException(404, "來源資料已刪除")
        record = await h.data_store.find_file_by_id(document_id)
        text = await h.data_store.get_file_content(document_id) if record else None
        if record:
            record = await h.data_store.find_file_by_id(document_id)
            if not record:
                raise HTTPException(404, "來源資料已刪除")
        if record and (not is_published_record(record) or not h.check_file_permission(record, owner)):
            raise HTTPException(403, "來源資料已無法存取")
        if not text:
            from services.knowledge.vector_store import get_vector_store
            store = await asyncio.to_thread(get_vector_store)
            if hasattr(store, "get_document_chunks"):
                chunks = await asyncio.to_thread(store.get_document_chunks, document_id)
            else:
                raw = await asyncio.to_thread(store._collection.get, where={"document_id": document_id}, include=["documents", "metadatas"])
                chunks = [{**(meta or {}), "chunk_id": cid, "content": content}
                          for cid, content, meta in zip(raw.get("ids") or [], raw.get("documents") or [], raw.get("metadatas") or [])]
            if not chunks:
                raise HTTPException(422, "資料尚無可用文字，請先完成解析")
            for chunk in chunks:
                meta = {**(chunk.get("metadata") or {}), **chunk}
                if not record and meta.get("audience") not in {"school", "personal", "class", "grade"}:
                    raise HTTPException(403, "來源資料缺少存取權限")
                if not is_published_record(meta) or not h.check_file_permission(record or meta, owner):
                    raise HTTPException(403, "來源資料已無法存取")
            record = record or {**(chunks[0].get("metadata") or {}), **chunks[0], "id": document_id}
            chunks.sort(key=lambda c: (c.get("page", 1), c.get("char_start", 0)))
            text = "\n".join(f'--- 第{c.get("page", 1)}页 ---\n{c["content"]}' for c in chunks)
        return record, text

    async def sources(self, task):
        from services.stored_evidence import chunks_from_record
        h, owner = self.host, task["owner"]
        selected, selected_text = await self.document(task["payload"]["document_id"], owner)
        # Only explicit metadata/title evidence can establish cross-file scope.
        def identity(record, text=""):
            title = str(record.get("name", "")) + "\n" + text[:600]
            grade = record.get("audience_grade") or record.get("grade")
            match = re.search(r"高[一二三]|初[一二三]|小[一二三四五六]", title)
            grade = grade or (match.group() if match else "")
            subject = record.get("subject")
            if not subject:
                subject = next((k for k, v in h._SUBJECT_LABELS.items() if v in title), "")
            period = record.get("study_period")
            if not period:
                period = next((k for k, v in h._PERIOD_LABELS.items() if v in title), "")
            return grade, subject, period
        scope = identity(selected, selected_text)
        records = [selected]
        if all(scope):
            candidates = [r for r in await h.data_store.list_file_records()
                          if r["id"] != selected["id"] and is_published_record(r)
                          and h.check_file_permission(r, owner) and identity(r) == scope]
            candidates.sort(key=lambda r: (r.get("study_period") == scope[2], r.get("published_at") or r.get("uploaded_at") or ""), reverse=True)
            records.extend(candidates[:12])
        sources, budget = [], 24000
        # Select complete page spans instead of only the document's first 3000 characters.
        terms = set(re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,8}", selected_text[:8000]))
        for record in records:
            if budget < 200:
                break
            text = selected_text if record["id"] == selected["id"] else (await h.data_store.get_file_content(record["id"]) or "")
            current = await h.data_store.find_file_by_id(record["id"])
            if h.is_deleted_file_record({"document_id":record["id"]}):
                if record["id"] == selected["id"]:
                    raise HTTPException(404, "來源資料已刪除")
                continue
            if current and (not is_published_record(current) or not h.check_file_permission(current, owner)):
                if record["id"] == selected["id"]:
                    raise HTTPException(403, "來源資料已無法存取")
                continue
            if record["id"] != selected["id"] and (not current or identity(current) != scope):
                continue
            chunks = await asyncio.to_thread(chunks_from_record, record, text)
            pages = {}
            for chunk in chunks:
                pages.setdefault(chunk["page"], []).append(chunk)
            spans = []
            for page, items in pages.items():
                start = min(c["char_start"] for c in items if c.get("char_start") is not None) if any(c.get("char_start") is not None for c in items) else None
                end = max(c["char_end"] for c in items if c.get("char_end") is not None) if start is not None else None
                content = text[start:end] if start is not None else "\n".join(c["content"] for c in items)
                spans.append(dict(document_id=record["id"], filename=record.get("name") or record.get("filename") or "所選資料",
                                  page=page, chunk_id=items[0]["chunk_id"], content=content, source_hash=fingerprint(text),
                                  source_offset=start, chunk_ranges=[{"chunk_id":c["chunk_id"], "start":c.get("char_start"), "end":c.get("char_end")} for c in items]))
            if record["id"] != selected["id"]:
                spans.sort(key=lambda s: sum(term in s["content"] for term in terms), reverse=True)
            for span in spans:
                if len(span["content"]) <= budget and len(span["content"]) <= 8000:
                    sources.append(span)
                    budget -= len(span["content"])
                    if record["id"] != selected["id"] and len([s for s in sources if s["document_id"] == record["id"]]) >= 2:
                        break
        return selected, sources

    async def check_sources(self, task):
        questions = (task.get("result") or {}).get("questions") or task.get("partial") or []
        versions = {q["document_id"]: q["source_hash"] for q in questions if q.get("document_id") and q.get("source_hash")}
        for document_id, version in versions.items():
            _, text = await self.document(document_id, task["owner"])
            if fingerprint(text) != version:
                raise HTTPException(409, "來源內容已變更，請重新產生試題")

    async def launch(self, task, operation, session_id=None):
        key = task["id"]
        if key in self.workers and not self.workers[key].done():
            return
        worker = asyncio.create_task(self.run(key, operation, session_id))
        self.workers[key] = worker
        worker.add_done_callback(lambda finished: self.workers.pop(key, None) if self.workers.get(key) is finished else None)

    async def progress(self, task_id, **values):
        async with self.lock(task_id):
            task = await self.db("get", task_id)
            if not task or task["status"] == "cancelled" or task.get("pause_requested"):
                raise asyncio.CancelledError()
            await self.db("update", task_id, **values)

    async def run(self, task_id, operation, session_id):
        task = await self.db("get", task_id)
        semaphore = self.quiz_slots if task["kind"] == "quiz" else self.upload_slots
        try:
            async with semaphore:
                await self.progress(task_id, status="running", error=None)
                task = await self.db("get", task_id)
                if operation == "chat_quiz":
                    from services.chat_tutor import generate_chat_quiz
                    attempts = task.get("generation_attempts", 0) + 1
                    await self.progress(task_id, generation_attempts=attempts)
                    result = await generate_chat_quiz(self, task)
                    task["result"] = result
                    await self.check_sources(task)
                    await self.progress(task_id, result=result, status="succeeded", stage="已完成", seen=False,
                                        completed=1, total=1, unit="題")
                elif operation == "quiz":
                    await self.check_sources(task)
                    selected, sources = await self.sources(task)
                    result = await generate_school_quiz(**task["payload"], filename=selected.get("name") or "文件練習題",
                        sources=sources, api_config=self.host.API_CONFIG,
                        progress=lambda **v: self.progress(task_id, **v), previous=task.get("partial"))
                    task["result"] = result
                    await self.check_sources(task)
                    await self.progress(task_id, stage="正在保存結果", total=None)
                    await self.progress(task_id, result=result, partial=[], status="succeeded", stage="已完成", seen=False,
                                        completed=result["count"], total=result["count"], unit="題")
                elif operation == "analyze":
                    await self.analyze(task)
                elif operation == "commit":
                    await self.commit(task, session_id)
        except asyncio.CancelledError:
            current = await self.db("get", task_id)
            if current and current["status"] in ACTIVE:
                await self.db("update", task_id, status="interrupted", stage="處理已中斷", error="可重試繼續處理")
        except Exception as error:
            if isinstance(error, (QuizGenerationError, QuizValidationError)):
                current = await self.db("get", task_id)
                logging.warning("Background task incomplete id=%s kind=%s completed=%s total=%s category=%s",
                                task_id, task["kind"], (current or {}).get("completed"),
                                (current or {}).get("total"), type(error).__name__)
            else:
                logging.exception("Background task failed: %s", task_id)
            message = str(error.detail) if isinstance(error, HTTPException) else str(error) if isinstance(error, (QuizValidationError, QuizGenerationError)) else "處理未完成，請重試"
            current = await self.db("get", task_id)
            if current and current["status"] != "cancelled":
                await self.db("update", task_id, status="failed", stage="處理失敗", error=message, seen=False)

    async def analyze(self, task):
        h, key = self.host, task["id"]
        raw_path = self.directory(key) / "original"
        if not raw_path.exists():
            raise QuizGenerationError("暫存原件已過期，請重新選擇檔案")
        loop = asyncio.get_running_loop()
        events = asyncio.Queue()
        stopped = threading.Event()
        def callback(event):
            if stopped.is_set():
                raise RuntimeError("Upload analysis cancelled")
            loop.call_soon_threadsafe(events.put_nowait, event)
        async def extract():
            with raw_path.open("rb") as handle:
                source = UploadFile(filename=task["name"], file=handle)
                return await h.parse_upload_isolated(source, progress_callback=callback, retry=False)
        work = asyncio.create_task(extract())
        try:
            await self.progress(key, stage="正在提取文字", total=None, unit="頁")
            while not work.done():
                try:
                    event = await asyncio.wait_for(events.get(), .2)
                except asyncio.TimeoutError:
                    continue
                if event.get("type") in {"ocr_started", "ocr_progress"}:
                    await self.progress(key, stage="OCR 識別", completed=event.get("completed", 0), total=event.get("total"), unit="頁")
            extraction = await work
        finally:
            if not work.done():
                stopped.set()
                try:
                    await asyncio.shield(work)
                except Exception:
                    pass
        content, truncated = h.truncate_text(extraction.get("content") or "")
        extraction = dict(content=content, is_truncated=truncated, page_count=extraction.get("page_count", 0),
                          extraction_status="failed" if extraction.get("error") else extraction.get("extraction_status", "none"))
        await self.db("update", key, extraction=extraction)
        await self.progress(key, stage="AI 正在填寫分類", total=None)
        suggestion = {}
        if content:
            try:
                suggestion = await h.classify_upload(task["name"], content, h.API_CONFIG)
            except Exception:
                pass  # Manual classification is still available after a provider failure.
        suggestion = h.apply_classification_defaults(suggestion, h.datetime.now().month)
        defaults = suggestion.pop("classification_defaults", {})
        if "study_period" in defaults:
            suggestion["study_period"] = h.study_period_for_month(h.datetime.now().month)
        evidence = suggestion.pop("classification_evidence", {})
        await self.db("merge_suggestions", key, {k: v for k, v in suggestion.items() if k in DRAFT_FIELDS})
        await self.db("update", key, classification_evidence=evidence, classification_defaults=defaults)
        await self.progress(key, status="waiting_confirmation", stage="待確認分類", total=None, error=None)

    async def commit(self, task, session_id):
        h, key = self.host, task["id"]
        # A committed file may outlive a failed task-state write; reconcile before retrying.
        existing = next((r for r in await h.data_store.list_file_records() if r.get("owner") == task["owner"] and r.get("upload_request_id") == task["request_id"]), None)
        if existing:
            file_id = existing["id"]
        else:
            await self.progress(key, stage="正在保存檔案", total=None, commit_started=True)
            raw = await asyncio.to_thread((self.directory(key) / "original").read_bytes)
            extraction = task.get("extraction") or dict(content="", page_count=0, extraction_status="failed", is_truncated=False)
            token = managed_upload.set(task["owner"])
            try:
                source = UploadFile(filename=task["name"], file=io.BytesIO(raw))
                values = {k: task["draft"].get(k, "") for k in DRAFT_FIELDS}
                result = await h.upload_file(file=source, **values, quick_upload="false", extraction_result=json.dumps(extraction),
                    extraction_signature=h.sign_extraction(extraction, fingerprint(raw), session_id),
                    upload_request_id=task["request_id"], session_id=session_id)
                file_id = result["data"]["file_id"]
            finally:
                managed_upload.reset(token)
            existing = await h.data_store.find_file_by_id(file_id)
        await self.progress(key, result={"file_id": file_id, "review_status": existing.get("review_status", "published")},
                            committed_file_id=file_id, commit_started=False)
        content = await h.data_store.get_file_content(file_id) or ""
        if is_published_record(existing):
            await self.progress(key, stage="正在建立全文語意索引", total=None)
            await h._index_fulltext_background(existing, content)
        if content.strip() and is_published_record(existing):
            await self.progress(key, stage="正在建立檢索索引", total=None)
            from services.knowledge.vector_store import get_vector_store
            store = await asyncio.to_thread(get_vector_store)
            loop = asyncio.get_running_loop()
            def callback(completed, total):
                # Sequential acknowledgement preserves exact progress before completion.
                future = asyncio.run_coroutine_threadsafe(self.progress(key, stage="建立檢索索引", completed=completed, total=total, unit="段落"), loop)
                future.result()
            try:
                await asyncio.to_thread(store.index_document, existing, content, progress_callback=callback)
            except Exception as exc:
                await self.progress(key, status="failed", stage="檔案已保存，索引失敗", error="原件已保存，可重試建立索引", seen=False)
                return
        await self.progress(key, status="succeeded", stage="已提交審核" if not is_published_record(existing) else "已完成", seen=False, total=None)
        await asyncio.to_thread(lambda: (self.directory(key) / "original").unlink(missing_ok=True))

    async def stop(self):
        workers = list(self.workers.values())
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)


def install(app, host):
    controller = None
    def get():
        nonlocal controller
        data_root = Path(host.DATA_DIR)
        source_root = Path(__file__).resolve().parents[1]
        conversation_root = Path(getattr(host, "CONVERSATIONS_FILE", data_root / "conversations_data.json")).parent
        if data_root.resolve() == source_root and conversation_root.resolve() != source_root:
            data_root = conversation_root
        root = data_root / "storage" / "background_tasks"
        if controller is None or controller.store.root != root:
            controller = TaskController(host, data_root)
        return controller
    host.get_task_controller = get

    async def account(session_id):
        session = await host.data_store.get_session(session_id)
        return session["account"]

    def request_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", value):
            raise HTTPException(422, "無效的請求編號")
        return value

    @app.get("/api/tasks")
    async def tasks(session_id: str = Depends(host.verify_session)):
        c = get()
        if time.monotonic() - c.last_cleanup > 3600:
            c.last_cleanup = time.monotonic()
            await c.db("expire_drafts")
        return {"code": 200, "data": [public_task(t) for t in await c.db("list", await account(session_id)) if t.get("origin") != "chat"]}

    @app.post("/api/tasks/quizzes", status_code=202)
    async def quiz(body: dict = Body(...), session_id: str = Depends(host.verify_session)):
        c = get()
        try:
            options = host.QuizGenerateRequest(**{k: v for k, v in body.items() if k != "request_id"})
            validate_options(options.question_types, options.count, options.difficulty)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "請檢查出題設定") from exc
        await host._authorize_document(options.document_id, session_id)
        values = options.model_dump()
        record = await host.data_store.find_file_by_id(options.document_id)
        try:
            task, created = await c.db("create", await account(session_id), "quiz", request_id(body.get("request_id")),
                                       fingerprint(values), values, (record or {}).get("name") or "文件練習題")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if created:
            await c.launch(task, "quiz")
        return {"code": 200, "data": public_task(task)}

    @app.post("/api/tasks/uploads", status_code=202)
    async def upload(file: UploadFile = File(...), request_id_value: str = Form(..., alias="request_id"),
                     session_id: str = Depends(host.verify_session)):
        c, owner = get(), await account(session_id)
        rid = request_id(request_id_value)
        if not file.filename or Path(file.filename).suffix.lower() not in ({".pdf", ".txt", ".docx", ".pptx"} | PHOTO_EXTENSIONS):
            raise HTTPException(422, "請選擇支援的資料檔案")
        raw = await file.read(host.APP_CONFIG.get("MAX_UPLOAD_SIZE", 50 * 1024 * 1024) + 1)
        if len(raw) > host.APP_CONFIG.get("MAX_UPLOAD_SIZE", 50 * 1024 * 1024):
            raise HTTPException(413, "檔案過大")
        try:
            task, created = await c.db("create", owner, "upload", rid, fingerprint(raw), {}, Path(file.filename).name)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if created:
            directory = c.directory(task["id"])
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
            try:
                await asyncio.to_thread((directory / "original").write_bytes, raw)
            except OSError as exc:
                await c.db("update", task["id"], status="failed", error="原件保存失敗，請重新選擇檔案")
                raise HTTPException(500, "原件保存失敗") from exc
            await c.launch(task, "analyze")
        return {"code": 200, "data": public_task(task)}

    @app.get("/api/tasks/{task_id}")
    async def detail(task_id: str, session_id: str = Depends(host.verify_session)):
        c = get()
        task = await c.owned(task_id, session_id)
        if task["kind"] == "quiz" and task.get("result"):
            await c.check_sources(task)
        return {"code": 200, "data": await c.upload_detail(task)}

    @app.post("/api/tasks/{task_id}/seen")
    async def seen(task_id: str, session_id: str = Depends(host.verify_session)):
        c = get()
        await c.owned(task_id, session_id)
        await c.db("update", task_id, seen=True)
        return {"code": 200}

    @app.patch("/api/tasks/{task_id}/draft")
    async def draft(task_id: str, body: dict = Body(...), session_id: str = Depends(host.verify_session)):
        c = get()
        async with c.lock(task_id):
            task = await c.owned(task_id, session_id)
            if task["kind"] != "upload" or task["status"] not in {"queued", "running", "waiting_confirmation"} or task.get("commit_started") or task.get("operation") == "commit":
                raise HTTPException(409, "此任務目前不能修改分類")
            values = body.get("values", {})
            if not isinstance(values, dict) or not set(values) <= DRAFT_FIELDS or any(not isinstance(v, str) or len(v) > 100 for v in values.values()):
                raise HTTPException(422, "分類欄位無效")
            if any(len(values.get(k, "").strip()) > 50 for k in ("subject_custom", "material_custom")) or len(values.get("notes", "").strip()) > 5:
                raise HTTPException(422, "自訂名稱最多50字，備註最多5字")
            values = {k: v.strip() for k, v in values.items()}
            client_id, sequence = body.get("client_id"), body.get("sequence")
            if client_id is not None and (not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", client_id) or type(sequence) is not int or not 0 <= sequence <= 10**9):
                raise HTTPException(422, "分類儲存版本無效")
            task = await c.db("draft", task_id, values, list(values), client_id, sequence)
            return {"code": 200, "data": await c.upload_detail(task)}

    @app.post("/api/tasks/{task_id}/commit", status_code=202)
    async def commit(task_id: str, body: dict = Body(default={}), session_id: str = Depends(host.verify_session)):
        c = get()
        async with c.lock(task_id):
            task = await c.owned(task_id, session_id)
            if task["kind"] != "upload":
                raise HTTPException(409, "非上傳任務")
            if task["status"] == "succeeded" or task.get("commit_started") or task["status"] in ACTIVE:
                return {"code": 200, "data": public_task(task)}
            if task["status"] != "waiting_confirmation":
                raise HTTPException(409, "請先完成資料分析")
            values = task["draft"]
            if any(not values.get(k) for k in ("audience", "subject", "study_period", "material_type")):
                raise HTTPException(422, "請填寫歸屬、科目、學習階段和資料類型")
            for key, options in (("audience", host._AUDIENCE_LABELS), ("subject", host._SUBJECT_LABELS),
                                 ("study_period", host._PERIOD_LABELS), ("material_type", host._MATERIAL_LABELS)):
                if values[key] not in options:
                    raise HTTPException(422, "分類選項無效，請重新選擇")
            if len(values.get("notes", "")) > 5 or any(len(values.get(k, "")) > 50 for k in ("material_custom", "subject_custom")):
                raise HTTPException(422, "備註最多5字，資料類型最多50字")
            if host.is_teacher_account(task["owner"]) and values["audience"] in {"class", "grade"}:
                if values.get("target_class") not in host.public_user_info(task["owner"]).get("publish_classes", []):
                    raise HTTPException(422, "請選擇發布資料的目標班級")
            await c.db("update", task_id, status="queued", operation="commit", stage="等待保存", total=None)
            await c.launch(task, "commit", session_id)
            return {"code": 200}

    @app.post("/api/tasks/{task_id}/resume", status_code=202)
    @app.post("/api/tasks/{task_id}/retry", status_code=202)
    async def retry(task_id: str, session_id: str = Depends(host.verify_session)):
        c = get()
        async with c.lock(task_id):
            task = await c.owned(task_id, session_id)
            if task["status"] not in {"failed", "interrupted"}:
                raise HTTPException(409, "此任務不需要重試")
            operation = "chat_quiz" if task.get("origin") == "chat" else "quiz" if task["kind"] == "quiz" else "commit" if task.get("committed_file_id") or task.get("operation") == "commit" else "analyze"
            await c.db("update", task_id, status="queued", error=None, pause_requested=False, stage="等待繼續" if task.get("pause_requested") else "等待重試")
            await c.launch(task, operation, session_id)
            return {"code": 200}

    @app.post("/api/tasks/{task_id}/pause")
    async def pause(task_id: str, session_id: str = Depends(host.verify_session)):
        c = get()
        async with c.lock(task_id):
            task = await c.owned(task_id, session_id)
            if task["kind"] != "quiz" or task["status"] not in ACTIVE:
                raise HTTPException(409, "目前只有處理中的出題任務可以暫停。")
            await c.db("update", task_id, status="interrupted", pause_requested=True, stage="已暫停，已核對題目保留", error=None)
            worker = c.workers.get(task_id)
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            return {"code": 200}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str, session_id: str = Depends(host.verify_session)):
        c = get()
        async with c.lock(task_id):
            task = await c.owned(task_id, session_id)
            if task.get("commit_started"):
                raise HTTPException(409, "正在保存原件，請稍後再操作")
            await c.db("update", task_id, status="cancelled", stage="已取消")
            worker = c.workers.get(task_id)
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            return {"code": 200}

    @app.delete("/api/tasks/{task_id}")
    async def delete(task_id: str, session_id: str = Depends(host.verify_session)):
        c = get()
        async with c.lock(task_id):
            task = await c.owned(task_id, session_id)
            if task["status"] in ACTIVE or task_id in c.workers:
                raise HTTPException(409, "請先取消任務再刪除")
            await c.db("delete", task_id)
            directory = c.directory(task_id)
            if directory.exists():
                await asyncio.to_thread(shutil.rmtree, directory)
            return {"code": 200}

    async def practice(task_id, attempt_id, session_id):
        c = get()
        task = await c.owned(task_id, session_id)
        if task["kind"] != "quiz" or task["status"] != "succeeded":
            raise HTTPException(409, "試題尚未完成")
        await c.check_sources(task)
        attempt = await c.db("attempt", task_id, attempt_id)
        if not attempt:
            raise HTTPException(404, "未找到作答記錄")
        return c, task, attempt

    @app.post("/api/tasks/{task_id}/attempts")
    async def new_attempt(task_id: str, body: dict = Body(default={}), session_id: str = Depends(host.verify_session)):
        c = get()
        task = await c.owned(task_id, session_id)
        if task["kind"] != "quiz" or task["status"] != "succeeded":
            raise HTTPException(409, "試題尚未完成")
        await c.check_sources(task)
        mode = body.get("mode", "instant")
        if mode not in {"instant", "exam"}:
            raise HTTPException(422, "答題模式無效")
        async with c.lock("new-attempt:" + task_id):
            if body.get("resume") is True:
                existing = await c.db("attempts", task_id)
                matching = next((a for a in existing if a.get("mode") == mode), None)
                if matching:
                    return {"code": 200, "data": matching}
            attempt = dict(id=uuid4().hex, mode=mode, answers={}, grades={}, submitted=False, created_at=now(), updated_at=now(),
                           learning_epoch=await c.db("learning_epoch", task["owner"]))
            return {"code": 200, "data": await c.db("save_attempt", task_id, attempt)}

    @app.get("/api/tasks/{task_id}/attempts")
    async def attempts(task_id: str, session_id: str = Depends(host.verify_session)):
        c = get()
        task = await c.owned(task_id, session_id)
        await c.check_sources(task)
        return {"code": 200, "data": await c.db("attempts", task_id)}

    @app.get("/api/tasks/{task_id}/attempts/{attempt_id}")
    async def get_attempt(task_id: str, attempt_id: str, session_id: str = Depends(host.verify_session)):
        _, _, attempt = await practice(task_id, attempt_id, session_id)
        return {"code": 200, "data": attempt}

    @app.patch("/api/tasks/{task_id}/attempts/{attempt_id}")
    async def save_attempt(task_id: str, attempt_id: str, body: dict = Body(...), session_id: str = Depends(host.verify_session)):
        c = get()
        async with c.lock(attempt_id):
            c, task, attempt = await practice(task_id, attempt_id, session_id)
            if attempt["submitted"]:
                raise HTTPException(409, "此份作答已提交，請重新練習")
            client_id, sequence = body.get("client_id"), body.get("sequence")
            if client_id is not None:
                if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", client_id) or type(sequence) is not int or not 0 <= sequence <= 10**9:
                    raise HTTPException(422, "作答儲存版本無效")
                clients = attempt.setdefault("save_clients", {})
                if sequence <= clients.get(client_id, -1):
                    return {"code": 200, "data": attempt}
                clients[client_id] = sequence
            answers = body.get("answers")
            if not isinstance(answers, dict) or any(not str(k).isdigit() or not 0 <= int(k) < len(task["result"]["questions"]) or not isinstance(v, str) or len(v) > 4000 for k, v in answers.items()):
                raise HTTPException(422, "作答內容無效")
            if any(k in attempt["grades"] and v != attempt["answers"].get(k) for k, v in answers.items()):
                raise HTTPException(409, "已判題的答案不能修改，請重新練習")
            attempt["answers"].update(answers)
            attempt["updated_at"] = now()
            return {"code": 200, "data": await c.db("save_attempt", task_id, attempt)}

    @app.post("/api/tasks/{task_id}/attempts/{attempt_id}/grade-question")
    async def grade(task_id: str, attempt_id: str, body: dict = Body(...), session_id: str = Depends(host.verify_session)):
        c = get()
        async with c.lock(attempt_id):
            c, task, attempt = await practice(task_id, attempt_id, session_id)
            index = body.get("index")
            if type(index) is not int or not 0 <= index < len(task["result"]["questions"]) or attempt["mode"] != "instant":
                raise HTTPException(422, "判題請求無效")
            key = str(index)
            if key not in attempt["grades"]:
                answer = attempt["answers"].get(key, "")
                if not answer.strip():
                    raise HTTPException(422, "請先填寫答案")
                try:
                    result = await grade_answer(task["result"]["questions"][index], answer, host.API_CONFIG)
                except (QuizGenerationError, QuizValidationError) as exc:
                    raise HTTPException(502, str(exc)) from exc
                await c.check_sources(task)
                attempt["grades"][key] = result
                await c.db("save_attempt", task_id, attempt)
            return {"code": 200, "data": attempt}

    @app.post("/api/tasks/{task_id}/attempts/{attempt_id}/submit")
    async def submit(task_id: str, attempt_id: str, body: dict = Body(default={}), session_id: str = Depends(host.verify_session)):
        c = get()
        async with c.lock(attempt_id):
            c, task, attempt = await practice(task_id, attempt_id, session_id)
            if attempt["submitted"]:
                return {"code": 200, "data": attempt}
            questions = task["result"]["questions"]
            if any(not attempt["answers"].get(str(i), "").strip() for i in range(len(questions))) and body.get("allow_unanswered") is not True:
                raise HTTPException(409, "仍有未作答題目，請確認是否提交")
            grades = dict(attempt["grades"])
            try:
                for index, question in enumerate(questions):
                    if str(index) not in grades:
                        grades[str(index)] = await grade_answer(question, attempt["answers"].get(str(index), ""), host.API_CONFIG)
                await c.check_sources(task)
            except (QuizGenerationError, QuizValidationError) as exc:
                # Exam mode must not leak partial answers on a failed submit.
                raise HTTPException(502, "判題未完成，作答已保存，請重試") from exc
            attempt.update(grades=grades, submitted=True, updated_at=now(), correct=sum(g["correct"] for g in grades.values()), total=len(questions))
            return {"code": 200, "data": await c.db("save_attempt", task_id, attempt)}

    @app.get("/api/tasks/{task_id}/pdf")
    async def pdf(task_id: str, answers: bool = False, session_id: str = Depends(host.verify_session)):
        from services.task_pdf import export_quiz_pdf
        c = get()
        async with c.lock("pdf:" + task_id):
            task = await c.owned(task_id, session_id)
            if task["kind"] != "quiz" or task["status"] != "succeeded":
                raise HTTPException(409, "試題尚未完成")
            if task.get("origin") == "chat":
                raise HTTPException(409, "聊天練習請在對話中作答及查看解析")
            await c.check_sources(task)
            directory = c.directory(task_id)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / ("answers.pdf" if answers else "paper.pdf")
            if not path.exists():
                await asyncio.to_thread(export_quiz_pdf, task["result"], path, answers=answers)
            await c.check_sources(task)
            return FileResponse(path, media_type="application/pdf", filename=task["name"].rsplit(".", 1)[0] + ("_答案解析.pdf" if answers else "_試卷.pdf"))
