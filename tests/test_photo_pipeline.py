import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from pydantic import ValidationError

import tests  # Establish synthetic-only storage before application imports.
from services import chat_attachments, fulltext, photo_pipeline
from utils.image_utils import prepare_image_bytes


class PhotoPreviewTests(unittest.TestCase):
    def test_large_rotated_jpg_is_cached_and_original_remains_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rotated.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.effect_noise((1200, 2400), 40).save(source, quality=96, exif=exif)
            original = source.read_bytes()
            cache = root / "photo-cache"
            preview = photo_pipeline.preview_path(str(source), source.name, "screen", cache)
            with Image.open(preview) as image:
                self.assertEqual(image.size, (1600, 800))
            self.assertLess(Path(preview).stat().st_size, len(original))
            self.assertIsNone(photo_pipeline.preview_path(str(source), source.name, "detail", cache))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(photo_pipeline.preview_path(str(source), source.name, "screen", cache), preview)
            source.touch()
            self.assertNotEqual(photo_pipeline.preview_path(str(source), source.name, "screen", cache), preview)

    def test_small_jpeg_uses_original_and_ocr_preparation_is_byte_preserving(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "small.jpg"
            Image.new("RGB", (220, 140), "green").save(source)
            original = source.read_bytes()
            self.assertIsNone(photo_pipeline.preview_path(str(source), source.name, "screen", root / "cache"))
            self.assertIs(prepare_image_bytes(original, compact_jpeg=True), original)

    def test_existing_ocr_is_indexed_without_opening_image(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(fulltext, "_encode") as encode:
            import numpy as np
            encode.side_effect = lambda text: np.ones((len(text), 3), dtype=np.float32)
            index = fulltext.FullTextIndex(directory)
            record = {"id": "old-photo", "name": "old.jpg", "extraction_status": "success"}
            ocr = Mock(side_effect=AssertionError("Search must use stored OCR"))
            self.assertEqual(index.ensure(record, "missing-original.jpg", "ORIGINAL OCR P7Q4R2", ocr), [])
            self.assertEqual(index.document_passages(record)[0]["location"], "照片 OCR")
            self.assertIn("P7Q4R2", index.document_passages(record)[0]["content"])
            ocr.assert_not_called()
            record.update(id="blank-photo", extraction_status="empty")
            self.assertEqual(index.ensure(record, None, "", ocr), [])


class PhotoFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_camera_can_send_a_photo_without_caption(self):
        from main import QuestionRequest

        self.assertEqual(QuestionRequest(session_id="test", file_ids=["photo"]).question, "")
        with self.assertRaises(ValidationError):
            QuestionRequest(session_id="test")

    async def test_pasted_document_commits_as_personal_with_deferred_extraction(self):
        from datetime import datetime

        async def upload(**values):
            self.assertTrue(chat_attachments.deferred_attachment_upload.get())
            self.assertEqual(await values["file"].read(), b"document evidence")
            self.assertEqual(values["audience"], "personal")
            self.assertEqual(values["upload_request_id"], "attachment-test-attachment")
            return {"file_id": "saved-attachment"}

        host = SimpleNamespace(upload_file=upload, APP_CONFIG={"MAX_UPLOAD_SIZE": 1024},
                               datetime=datetime, study_period_for_month=lambda _: "first")
        result = await chat_attachments.save_chat_attachment(
            host, raw=b"document evidence", name="notes.txt", request_id="test-attachment", session_id="test",
        )
        self.assertEqual(result["file_id"], "saved-attachment")
        self.assertFalse(chat_attachments.deferred_attachment_upload.get())

    async def test_pending_ocr_recovery_and_duplicate_enqueue(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "photo.jpg"
            Image.new("RGB", (40, 40)).save(raw)
            record = {"id": "photo", "original_name": "photo.jpg", "original_sha256": "digest",
                      "extraction_status": "pending", "upload_request_id": "photo-request"}
            store = SimpleNamespace(list_file_records=AsyncMock(return_value=[record]),
                                    find_file_by_id=AsyncMock(return_value=record),
                                    update_attachment_text=AsyncMock(return_value=record))
            host = SimpleNamespace(data_store=store, resolve_storage_path=lambda *_: str(raw),
                                   parse_upload_isolated=AsyncMock(return_value={"content": "stored", "extraction_status": "success"}),
                                   truncate_text=lambda text: (text, False), hash_text=lambda _: "text-hash",
                                   _index_file_background=AsyncMock())
            worker = chat_attachments.ChatAttachmentProcessor(host)
            await worker.recover()
            worker.schedule(record)
            await asyncio.gather(*worker.jobs.values())
            host.parse_upload_isolated.assert_awaited_once()
            store.update_attachment_text.assert_awaited_once()
            self.assertEqual(store.update_attachment_text.await_args.args[-1], "stored")
            host._index_file_background.assert_awaited_once()

    async def test_only_scanned_pdf_dependencies_wait_for_background_text(self):
        import fitz

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = root / "native.pdf"
            scanned = root / "scan.pdf"
            image = root / "photo.png"
            Image.new("RGB", (120, 120), "white").save(image)
            with fitz.open() as pdf:
                pdf.new_page().insert_text((40, 40), "native text")
                pdf.save(native)
            with fitz.open() as pdf:
                page = pdf.new_page()
                page.insert_image(page.rect, filename=str(image))
                pdf.save(scanned)
            records = {"native": {"original_name": native.name}, "scan": {"original_name": scanned.name}}
            host = SimpleNamespace(data_store=SimpleNamespace(find_file_by_id=AsyncMock(side_effect=records.get)),
                                   resolve_storage_path=lambda _, record: str(root / record["original_name"]))
            worker = chat_attachments.ChatAttachmentProcessor(host)
            worker.text_ready.update({key: asyncio.Event() for key in records})
            pending = await worker.pending_scans(["native", "scan"])
            self.assertEqual(pending, [worker.text_ready["scan"]])
            pending[0].set()
            await chat_attachments.wait_for_scan_text(pending)

    async def test_vision_is_attempted_with_image_and_fallback_uses_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "image.jpg"
            Image.new("RGB", (280, 280), "blue").save(source)
            photos = [{"id": "example", "name": source.name, "path": str(source), "text": ""}]
            calls = []

            async def stream(prompt, question, **kwargs):
                calls.append((prompt, kwargs))
                if kwargs.get("images"):
                    yield photo_pipeline.photo_event("error", detail="fixture")
                else:
                    yield photo_pipeline.photo_event("content", text="OCR reply")

            host = SimpleNamespace(stream_llm=stream, resolve_chat_model=lambda _: SimpleNamespace(name="DeepSeek"),
                                   data_store=SimpleNamespace(get_file_content=AsyncMock(return_value="OCR evidence")))
            events = [json.loads(value[6:]) async for value in photo_pipeline.stream_photo_reply(
                host, photos, "prompt", "question", think=False, model="openpangu-2.0-flash",
            )]
            self.assertEqual(calls[0][1]["model"], "deepseek-v4.1-flash-1017")
            self.assertTrue(calls[0][1]["images"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            self.assertEqual(calls[1][1]["model"], "openpangu-2.0-flash")
            self.assertIn("OCR evidence", calls[1][0])
            self.assertIn("ocr_fallback", [event.get("status") for event in events])


if __name__ == "__main__":
    unittest.main()
