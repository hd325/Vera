"""Save pasted chat originals quickly and extract searchable text in the background."""
from __future__ import annotations

import asyncio
import contextvars
import io
import logging
import re
from pathlib import Path

from fastapi import HTTPException, UploadFile

from services.photo_pipeline import normalized_photo_name
from utils.image_utils import PHOTO_EXTENSIONS, photo_name

DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".markdown", ".csv", ".tsv", ".json"})
ATTACHMENT_EXTENSIONS = DOCUMENT_EXTENSIONS | PHOTO_EXTENSIONS
ATTACHMENT_ONLY_QUESTION = "請查看這則訊息的附件，簡要說明內容。"
deferred_attachment_upload = contextvars.ContextVar("deferred_attachment_upload", default=False)
log = logging.getLogger(__name__)


async def save_chat_attachment(host, *, raw: bytes, name: str, request_id: str, session_id: str, photos_only=False):
    is_photo = photo_name(name)
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,55}", request_id or "") or (not is_photo and len(request_id) > 53):
        raise HTTPException(422, "無效的附件上傳請求。")
    if not name or Path(name).name != name or re.search(r"[\\/:\x00-\x1f]", name) or len(name) > 180:
        raise HTTPException(422, "附件檔名不支援。")
    if not raw or len(raw) > host.APP_CONFIG["MAX_UPLOAD_SIZE"]:
        raise HTTPException(413, "附件為空或超過 50 MB。")
    allowed = PHOTO_EXTENSIONS if photos_only else ATTACHMENT_EXTENSIONS
    if Path(name).suffix.lower() not in allowed:
        raise HTTPException(422, "此附件格式暫不支援。")
    if is_photo:
        name = await asyncio.to_thread(normalized_photo_name, name, raw)
    request_prefix = "photo-" if is_photo else "attachment-"
    token = deferred_attachment_upload.set(True)
    try:
        return await host.upload_file(
            file=UploadFile(filename=name, file=io.BytesIO(raw)), audience="personal", subject="other",
            subject_custom="", material_type="other", material_custom="照片" if is_photo else "對話附件",
            study_period=host.study_period_for_month(host.datetime.now().month), notes="", target_class="",
            quick_upload="false", extraction_result="", extraction_signature="",
            upload_request_id=request_prefix + request_id, session_id=session_id,
        )
    finally:
        deferred_attachment_upload.reset(token)


class ChatAttachmentProcessor:
    def __init__(self, host):
        self.host = host
        self.jobs = {}
        self.text_ready = {}
        self.slots = asyncio.Semaphore(2)

    def schedule(self, record):
        file_id = record["id"]
        if record.get("extraction_status") != "pending" or file_id in self.jobs:
            return
        ready = self.text_ready[file_id] = asyncio.Event()
        job = asyncio.create_task(self.run(file_id, ready))
        self.jobs[file_id] = job

        def finish(_):
            ready.set()
            self.jobs.pop(file_id, None)
            self.text_ready.pop(file_id, None)

        job.add_done_callback(finish)

    async def recover(self):
        for record in await self.host.data_store.list_file_records():
            if str(record.get("upload_request_id") or "").startswith(("photo-", "attachment-")):
                self.schedule(record)

    async def stop(self):
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    async def pending_scans(self, file_ids):
        """Wait for OCR only when a referenced PDF contains a scanned page."""
        host = self.host
        waits = []
        for file_id in file_ids:
            if file_id not in self.text_ready:
                continue
            record = await host.data_store.find_file_by_id(file_id)
            if not record or Path(record.get("original_name") or "").suffix.lower() != ".pdf":
                continue
            path = host.resolve_storage_path(record.get("storage_path"), record)
            if path and await asyncio.to_thread(_pdf_needs_ocr, path):
                ready = self.text_ready.get(file_id)
                if ready and not ready.is_set():
                    waits.append(ready)
        return waits

    async def run(self, file_id, ready):
        host = self.host
        try:
            async with self.slots:
                record = await host.data_store.find_file_by_id(file_id)
                if not record or record.get("extraction_status") != "pending":
                    return
                path = host.resolve_storage_path(record.get("storage_path"), record)

                async def extract():
                    if not path:
                        return {}
                    try:
                        with open(path, "rb") as handle:
                            return await host.parse_upload_isolated(
                                UploadFile(filename=record["original_name"], file=handle), retry=False,
                            )
                    except OSError:
                        return {}

                extraction = await extract()
                content, truncated = host.truncate_text(extraction.get("content") or "")
                status = "failed" if extraction.get("error") else extraction.get("extraction_status", "failed")
                updates = {"extraction_status": status, "is_truncated": truncated,
                           "page_count": extraction.get("page_count", 0),
                           "text_sha256": host.hash_text(content) if content and not truncated else ""}
                updated = await host.data_store.update_attachment_text(file_id, record["original_sha256"], updates, content)
            ready.set()
            if updated and content:
                await host._index_file_background(updated, content)
        except Exception as exc:
            log.warning("Chat attachment processing failed (%s)", type(exc).__name__)


def _pdf_needs_ocr(path):
    import pymupdf as fitz

    try:
        with fitz.open(path) as document:
            return any(not page.get_text().strip() and page.get_image_info() for page in document)
    except (OSError, RuntimeError, ValueError):
        return False


async def wait_for_scan_text(pending):
    try:
        async with asyncio.timeout(75):
            await asyncio.gather(*(ready.wait() for ready in pending))
    except TimeoutError:
        log.info("Referenced PDF OCR is still running")
