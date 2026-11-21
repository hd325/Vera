import tests  # Synthetic storage must precede application imports.
import unittest

from services.process_trace import (
    ProcessTracker,
    doc_step_links,
    file_step_links,
    memory_step_links,
    web_step_links,
)


class ProcessTraceTests(unittest.TestCase):
    def test_step_lifecycle_preserves_order_and_finalizes_running_steps(self):
        tracker = ProcessTracker(config={"model_name": "DeepSeek-V4.1-Flash"})
        first = tracker.step("plan", "plan", "running", "正在理解問題…")
        self.assertEqual(first["type"], "process_step")
        self.assertEqual(first["status"], "running")

        updated = tracker.step("plan", "plan", "done", "已理解問題", detail="校內資料", note="我正在查詢文件")
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["note"], "我正在查詢文件")

        tracker.step("answer", "answer", "running", "正在撰寫回覆…")
        trace = tracker.finalize("done")
        self.assertEqual([s["id"] for s in trace["steps"]], ["plan", "answer"])
        self.assertEqual(trace["steps"][1]["status"], "done")
        self.assertEqual(trace["steps"][1]["title"], "已撰寫回覆")
        self.assertEqual(trace["config"]["model_name"], "DeepSeek-V4.1-Flash")

    def test_link_builders_sanitize_and_deduplicate(self):
        web = web_step_links([
            {"title": "Good", "url": "https://www.example.com/a", "content_status": "fetched"},
            {"title": "Dup", "url": "https://www.example.com/a", "content_status": "snippet_only"},
            {"title": "Bad", "url": "javascript:alert(1)", "content_status": "fetched"},
        ])
        self.assertEqual(len(web), 1)
        self.assertEqual(web[0]["sublabel"], "example.com · 已讀正文")

        docs = doc_step_links([
            {"document_id": "d1", "chunk_id": "c1", "filename": "math.pdf", "page": 1},
            {"document_id": "d1", "chunk_id": "c2", "filename": "math.pdf", "page": 2},
            {"document_id": "d2", "chunk_id": "c3", "filename": "chem.pdf", "location": "第二章"},
        ])
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0]["label"], "math.pdf")
        self.assertIn("2 段", docs[0]["sublabel"])
        self.assertEqual(docs[1]["sublabel"], "第二章")

        files = file_step_links([{"id": "f1", "name": "notes.docx"}], sublabel="預覽")
        self.assertEqual(files, [{"kind": "file", "label": "notes.docx", "file_id": "f1", "sublabel": "預覽"}])

        mems = memory_step_links([{"id": "m1", "content": "偏好先看例子"}])
        self.assertEqual(mems, [{"kind": "memory", "label": "偏好先看例子", "memory_id": "m1"}])


if __name__ == "__main__":
    unittest.main()
