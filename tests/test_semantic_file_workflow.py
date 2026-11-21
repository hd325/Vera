import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import fitz
import numpy as np
from docx import Document
from openpyxl import Workbook, load_workbook
from pptx import Presentation
from pptx.util import Inches

import tests  # Establish synthetic-only storage before application imports.
from services.file_agent.skills import FileSkillError, FileSkills
from services.fulltext import FullTextIndex, _extract, parse_chunk_id
from services.turn_planner import _structured_reply, plan_turn, select_evidence


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_every_supported_format_indexes_tail_with_locator(self):
        pdf = self.root / "a.pdf"
        with fitz.open() as doc:
            doc.new_page().insert_text((20, 30), "cover")
            doc.new_page().insert_text((20, 30), "the tail is on page two")
            doc.save(pdf)

        word = self.root / "b.docx"
        doc = Document()
        doc.add_paragraph("cover")
        doc.add_table(rows=1, cols=1).cell(0, 0).text = "the tail in a Word table"
        doc.save(word)

        book = self.root / "c.xlsx"
        workbook = Workbook()
        workbook.active.append(["cover"])
        sheet = workbook.create_sheet("結算")
        sheet["D9"] = "the tail in worksheet D9"
        sheet["E9"] = "=2+2"
        workbook.save(book)
        workbook.close()

        slides = self.root / "d.pptx"
        deck = Presentation()
        deck.slides.add_slide(deck.slide_layouts[6])
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        table = slide.shapes.add_table(1, 1, Inches(1), Inches(1), Inches(2), Inches(1)).table
        table.cell(0, 0).text = "the tail in the PPT table"
        deck.save(slides)

        markdown = self.root / "e.md"
        markdown.write_text("# cover\n\n\nthe tail in Markdown", encoding="utf-8")
        documents = [
            (pdf, "第 2 頁"), (word, "第 2 段"), (book, "結算 · 第 9 列"),
            (slides, "第 2 張投影片"), (markdown, "第 4 行"),
        ]
        records = {}
        index = FullTextIndex(self.root)
        # Deterministic vectors isolate coverage and locators from model availability.
        def encode(texts):
            return np.array([[1, 0] if "tail" in text else [0, 1] for text in texts], dtype=np.float32)

        with patch("services.fulltext._encode", side_effect=encode):
            for path, _ in documents:
                record = {"id": path.stem, "name": path.name, "is_truncated": True}
                records[path.stem] = record
                self.assertEqual(index.ensure(record, str(path), "only an old excerpt"), [])
            found = index.candidates(records, ["tail"], limit=5)
            for path, location in documents:
                item = next(chunk for chunk in found if chunk["document_id"] == path.stem and "tail" in chunk["content"])
                self.assertEqual(item["location"], location)
                if path == book:
                    self.assertIn("E9 =2+2", item["content"])
                fid, version, position = parse_chunk_id(item["chunk_id"])
                ctx = index.context(records[fid], version, position)
                self.assertEqual(ctx["target"]["content"], item["content"])
            index.remove("e")
            self.assertFalse(any(chunk["document_id"] == "e" for chunk in index.candidates(records, ["tail"])))

    def test_partial_scan_and_missing_original_are_reported(self):
        pdf = self.root / "scan.pdf"
        with fitz.open() as doc:
            sample = doc.new_page()
            sample.insert_text((50, 50), "sample")
            raster = sample.get_pixmap()
        with fitz.open() as doc:
            for _ in range(2):
                new_page = doc.new_page()
                new_page.insert_image(new_page.rect, pixmap=raster)
            doc.save(pdf)
        gaps = []
        content = list(_extract(pdf, "--- 第1页 ---\n第一頁有保存的 OCR", lambda *args: "", gaps))
        self.assertEqual(content, [(1, "第 1 頁", "第一頁有保存的 OCR")])
        self.assertEqual(gaps, ["第 2 頁缺少辨識文字"])
        index = FullTextIndex(self.root)
        with patch("services.fulltext._encode", return_value=np.array([[1, 0]], dtype=np.float32)):
            gaps = index.ensure({"id": "missing", "name": "gone.md", "is_truncated": True}, None, "available")
        self.assertIn("保存正文有截斷", gaps)

    def test_short_itinerary_keeps_return_flight_when_departure_was_selected(self):
        index = FullTextIndex(self.root)
        itinerary = {"id": "travel", "name": "itinerary.pdf"}
        contents = "--- 第1頁 ---\n去程搭乘火車\n--- 第2頁 ---\n五班回程搭乘 NX001"
        with patch("services.fulltext._encode", return_value=np.array([[1, 0], [0, 1]], dtype=np.float32)):
            index.ensure(itinerary, None, contents)
        departure, arrival = index.document_passages(itinerary)
        matched = index.expand_short_sources({"travel": itinerary}, [departure])
        self.assertEqual([item["chunk_id"] for item in matched], [departure["chunk_id"], arrival["chunk_id"]])
        self.assertIn("NX001", matched[1]["content"])

    def test_excel_conversion_creates_native_cells_and_can_edit_a_copy(self):
        from services.file_agent.agent import export_text_content

        skills = FileSkills({}, self.root)
        source = "| 班級 | 航班 |\n| --- | --- |\n| 五班 | NX001 |\n| 三班 | NX005 |"
        created = skills.create_file("xlsx", "航班.xlsx", export_text_content(source, "xlsx"))
        edited = skills.edit_file(created["file_id"], "航班_修訂.xlsx",
                                  [{"op": "set_cell", "sheet": "表格 1", "cell": "B1", "value": "回程航班"}])
        for artifact, header in ((created, "航班"), (edited, "回程航班")):
            book = load_workbook(artifact["path"], read_only=True)
            try:
                self.assertEqual(list(book.active.values), [("班級", header), ("五班", "NX001"), ("三班", "NX005")])
            finally:
                book.close()

    def test_five_binaries_reopen_and_pdf_contains_embedded_cjk_and_math(self):
        skills = FileSkills({}, self.root)
        tokens = "練習卷：∩ ≤ √ α ₁ ₙ −"
        cases = {
            "docx": {"title": tokens, "paragraphs": ["第一題"]},
            "pdf": {"title": tokens, "paragraphs": ["第一題"]},
            "pptx": {"slides": [{"title": "練習卷", "body": ["第一題"]}]},
            "xlsx": {"sheets": [{"name": "題目", "rows": [[tokens]]}]},
            "md": {"text": f"# {tokens}\n第一題"},
        }
        artifacts = {kind: skills.create_file(kind, f"練習卷.{kind}", payload)
                     for kind, payload in cases.items()}
        self.assertTrue(all(item["validation"]["valid"] for item in artifacts.values()))
        with fitz.open(artifacts["pdf"]["path"]) as pdf:
            self.assertIn(tokens, pdf[0].get_text())
            self.assertTrue(all(pdf.extract_font(row[0])[3] for row in pdf[0].get_fonts()))
            self.assertEqual(pdf[0].get_pixmap().width, 595)
        edited = skills.edit_file(artifacts["pdf"]["file_id"], "加註.pdf",
                                  [{"op": "add_text", "page": 0, "text": "繁體和ₙ−", "x": 60, "y": 400}])
        with fitz.open(edited["path"]) as pdf:
            self.assertIn("繁體和ₙ−", pdf[0].get_text())
        result = skills.edit_file(artifacts["md"]["file_id"], "修訂.md",
                                  [{"op": "replace_text", "old": "第一題", "new": "修訂題"}])
        self.assertIn("修訂題", Path(result["path"]).read_text(encoding="utf-8"))
        self.assertIn("第一題", Path(artifacts["md"]["path"]).read_text(encoding="utf-8"))
        with self.assertRaises(FileSkillError):
            skills.create_file("pdf", "字形失敗.pdf", {"title": "缺\U0010ffff"})


class ModelContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_previous_answer_pdf_export_preserves_vocabulary_without_a_generation_call(self):
        from services.file_agent.integration import export_answer_text
        from services.turn_planner import _conversation_excerpt, normalize_plan

        markdown = "# 高三英語詞彙\n\n" + "\n\n".join(
            f"{number}. {word} /ˈænəlaɪz/ — 分析" for number, word in enumerate(
                ("Analyze", "Approach", "Context", "Define", "Evidence", "Factor", "Identify",
                 "Method", "Process", "Significant"), 1))
        previous = {"is_user": False, "content": "<think>" + "reasoning " * 100 + "</think>" + markdown}
        self.assertIn("Analyze", _conversation_excerpt([previous])[0]["text"])
        plan = normalize_plan({"action": "create", "source": "previous", "formats": ["pdf"], "export_previous": True})
        self.assertTrue(plan["export_previous"])
        self.assertFalse(normalize_plan({"action": "answer", "formats": ["pdf"], "export_previous": True})["export_previous"])
        with tempfile.TemporaryDirectory() as directory:
            skills = FileSkills({}, Path(directory))
            runtime = SimpleNamespace(call=AsyncMock(side_effect=skills.execute))
            authorize = AsyncMock()
            output_ids = await export_answer_text(runtime, markdown, plan["formats"], authorize)
            self.assertEqual(len(output_ids), 1)
            with fitz.open(next(Path(directory).glob("*.pdf"))) as document:
                text = "".join(page.get_text() for page in document)
                self.assertIn("Analyze /ˈænəlaɪz/ — 分析", text)
                self.assertIn("10. Significant", text)
            authorize.assert_awaited_once()

    async def test_final_text_pdf_is_delivered_after_one_tool_submission(self):
        from services.file_agent.agent import LLMFileAgent, TEXT_EXPORT

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills = FileSkills({}, root)
            runtime = SimpleNamespace(records={}, root=root, call=AsyncMock(side_effect=skills.execute),
                                      read_ids=set(), asset_ids=set())
            agent = LLMFileAgent(runtime, {}, AsyncMock())
            arguments = {"filename": "詞彙", "formats": ["md"], "markdown": "# 詞彙\n\n1. Analyze — 分析"}
            agent.complete = AsyncMock(return_value={"tool_calls": [{"id": "call_test", "function": {
                "name": TEXT_EXPORT, "arguments": json.dumps(arguments),
            }}]})
            events = [item async for item in agent.run("生成詞彙 PDF", [], formats=["pdf"])]
            self.assertEqual(events[-1]["type"], "result")
            agent.complete.assert_awaited_once()
            runtime.call.assert_awaited_once()
            with fitz.open(next(root.glob("*.pdf"))) as document:
                self.assertIn("Analyze", document[0].get_text())

    def test_file_tool_envelope_must_be_complete_and_allowed(self):
        from services.file_agent.agent import (
            TEXT_EXPORT, TEXT_EXPORT_TOOL, ToolProtocolError, provider_tool_message, word_content,
        )
        arguments = {"filename": "練習卷", "formats": ["docx", "md"], "markdown": "# 練習卷\n\n### 1. 求 x² ≥ 4 的解。"}
        envelope = json.dumps([{"name": TEXT_EXPORT, "arguments": arguments}], ensure_ascii=False)
        message = provider_tool_message({"content": "<|tool_call_start|>" + envelope}, [TEXT_EXPORT_TOOL])
        self.assertEqual(message["content"], "")
        call, = message["tool_calls"]
        self.assertEqual(json.loads(call["function"]["arguments"]), arguments)
        self.assertEqual(word_content(arguments["markdown"])["paragraphs"][-1]["text"], "1. 求 x² ≥ 4 的解。")
        for invalid in (envelope[:-2], envelope.replace(TEXT_EXPORT, "unapproved_operation")):
            with self.assertRaises(ToolProtocolError):
                provider_tool_message({"content": "<|tool_call_start|>" + invalid}, [TEXT_EXPORT_TOOL])

    def test_practice_sheet_numbers_questions_and_keeps_answers_separate(self):
        from jsonschema import ValidationError, validate
        from services.file_agent.agent import practice_markdown, practice_tool, word_content
        args = {"filename": "練習卷", "title": "練習卷",
                "questions": [{"text": "求 √4。", "answer": "2"}, {"text": "求 x² ≥ 4 的解。", "answer": "x ≤ −2 或 x ≥ 2"}]}
        schema = practice_tool(2)["function"]["parameters"]
        validate(args, schema)
        with self.assertRaises(ValidationError):
            validate({**args, "questions": args["questions"][:1]}, schema)
        markdown = practice_markdown(args)
        self.assertIn("### 第 2 題\n\n求 x² ≥ 4 的解。", markdown)
        paragraphs = word_content(markdown)["paragraphs"]
        self.assertEqual([item["text"] for item in paragraphs if item.get("style") == "Heading 3"],
                         ["第 1 題", "第 2 題"])

    async def test_practice_review_flags_math_markup_for_rewriting(self):
        from services.file_agent.agent import LLMFileAgent, practice_markdown
        checker = LLMFileAgent(None, {}, AsyncMock())
        checker.complete = AsyncMock(return_value={"content": json.dumps({
            "corrections": [{"number": 1, "text": "求 √4。", "answer": "2"}],
        })})
        draft = {"filename": "練習", "title": "練習",
                 "questions": [{"text": r"求 $\sqrt{4}$。", "answer": "2"}]}
        checked = await checker.review_practice("練習", draft, [])
        review_source = json.loads(checker.complete.await_args.args[0][-1]["content"])
        self.assertEqual(review_source["formula_rewrites"], [1])
        self.assertIn("求 √4。", practice_markdown(checked))

    def test_provider_analysis_followed_by_json(self):
        self.assertEqual(_structured_reply('analysis {unstructured}\n\n```json\n{"selected":[1,2]}\n```'),
                         {"selected": [1, 2]})

    def test_explicit_workbook_layout_blocks_raw_text_export(self):
        from services.turn_planner import normalize_plan

        schema = {"sheet": "返程航班", "headers": ["班級", "航班", "起飛", "抵達"]}
        plan = normalize_plan({"action": "create", "source": "previous", "formats": ["xlsx"],
                               "export_previous": True, "workbook": schema})
        self.assertFalse(plan["export_previous"])
        self.assertEqual(plan["workbook"], schema)

    async def test_native_office_executes_structured_creation_and_edit(self):
        from services.file_agent.agent import LLMFileAgent

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills = FileSkills({}, root)
            runtime = SimpleNamespace(call=AsyncMock(side_effect=skills.execute))
            agent = LLMFileAgent(runtime, {}, AsyncMock())
            sheet = {"name": "返程航班", "headers": ["班級", "航班"], "rows": [["高三5班", "NX001"]]}
            agent.complete = AsyncMock(side_effect=[
                {"content": json.dumps({"filename": "航班", "content": {"sheets": [sheet]}})},
                {"content": json.dumps({"filename": "航班修訂", "operations": [
                    {"op": "set_cell", "sheet": sheet["name"], "cell": "B1", "value": "返程航班編號"}
                ]})},
            ])
            first = await agent.execute_native_office({"current_request": "產生 Excel"}, "xlsx",
                                                      workbook={"sheet": sheet["name"], "headers": sheet["headers"]})
            changed = await agent.execute_native_office({"current_request": "修訂指定欄位"}, "xlsx",
                                                        workbook=None, source_id=first["file_id"])
            self.assertTrue(all(call.kwargs["structured"] for call in agent.complete.await_args_list))
            request = agent.complete.await_args.args[0]
            self.assertNotIn("output_file_ids", request[0]["content"])
            self.assertIn("operations", request[0]["content"])
            for path, label in ((first["path"], "航班"), (changed["path"], "返程航班編號")):
                book = load_workbook(path, read_only=True)
                try:
                    self.assertEqual(list(book.active.values), [("班級", label), ("高三5班", "NX001")])
                finally:
                    book.close()

    async def test_model_result_controls_route_and_bad_result_retries(self):
        response = AsyncMock(side_effect=[
            {"action": "answer", "source": "previous", "formats": ["docx"]},
            {"action": "create", "source": "previous", "formats": ["docx"]},
            {"action": "answer", "source": "none", "formats": []},
        ])
        with patch("services.turn_planner._request_plan", response):
            first = await plan_turn({"LLM_API_KEY": "test"}, "word文檔")
            second = await plan_turn({"LLM_API_KEY": "test"}, "word文檔")
        self.assertEqual((first["action"], first["formats"]), ("create", ["docx"]))
        self.assertEqual(second["action"], "answer")
        self.assertEqual(response.await_count, 3)

    async def test_file_replan_rejects_missing_previous_source(self):
        response = AsyncMock(side_effect=[
            {"action": "create", "source": "previous", "formats": ["docx"]},
            {"action": "create", "source": "none", "formats": ["docx"], "practice_questions": 10},
        ])
        with patch("services.turn_planner._request_plan", response):
            plan = await plan_turn({"LLM_API_KEY": "test"}, "整理成 Word", previous_source_missing=True)
        self.assertEqual((plan["source"], plan["formats"], plan["practice_questions"]), ("none", ["docx"], 10))

    async def test_evidence_tool_returns_only_supplied_passages(self):
        candidate = {"filename": "a.md", "location": "第 300 行", "content": "原文"}
        response = {"selected": [0, 1, 999, 1], "ambiguous": False, "clarification": "資料不足。"}
        with patch("services.turn_planner._request_plan", AsyncMock(return_value=response)):
            evidence, question = await select_evidence({}, "問題", [candidate])
        self.assertEqual(evidence, [candidate])
        self.assertEqual(question, "")

    async def test_general_knowledge_citations_require_verbatim_support(self):
        candidate = {"filename": "a.md", "location": "第 3 行", "content": "原文說明月相成因。"}
        responses = [
            {"selected": [1]},
            {"selected": [1], "direct_quotes": [{"n": 1, "quote": "不存在的解釋"}]},
            {"selected": [1], "direct_quotes": [{"n": 1, "quote": "原文說明月相成因"}]},
        ]
        with patch("services.turn_planner._request_plan", AsyncMock(side_effect=responses)):
            for expected in ([], [], [candidate]):
                evidence, _ = await select_evidence({}, "成因是甚麼？", [candidate], direct_only=True)
                self.assertEqual(evidence, expected)

    def test_file_lookup_does_not_prevent_general_chat(self):
        from main import _semantic_route, build_rag_prompt
        route = _semantic_route({"action": "answer", "source": "none"})
        self.assertFalse(route["retrieve_local"])
        prompt = build_rag_prompt("Word 文檔是甚麼", "", "", route=route,
                                  retrieval_status={"status": "skipped"})
        self.assertIn("請使用一般知識正常回答", prompt)
        self.assertNotIn("請明確告知目前無法依據文件庫回答", prompt)
        self.assertTrue(_semantic_route({"action": "answer", "source": "search"})["retrieve_local"])
        self.assertTrue(_semantic_route({"action": "answer", "source": "none"}, ["reference"])["retrieve_local"])
        self.assertFalse(_semantic_route({"action": "answer", "source": "none", "knowledge_scope": "none"})["retrieve_local"])
        probe = _semantic_route({"action": "answer", "source": "none", "knowledge_scope": "public"})
        self.assertTrue(probe["retrieve_local"])
        self.assertFalse(probe["requires_local"])

    async def test_original_delivery_honors_model_selected_format(self):
        from services.original_files import original_artifacts
        with tempfile.TemporaryDirectory() as folder:
            md = Path(folder) / "原始.markdown"
            md.write_text("正文", encoding="utf-8")
            records = {
                "word": {"name": "另一份.docx", "storage_path": "missing"},
                "md": {"name": md.name, "storage_path": str(md)},
            }
            host = SimpleNamespace(
                data_store=SimpleNamespace(find_file_by_id=AsyncMock(side_effect=records.get)),
                check_file_permission=lambda *_: True,
                resolve_storage_path=lambda path, record: path,
            )
            artifacts = await original_artifacts(host, "owner", records, formats=["md"])
        self.assertEqual([item["filename"] for item in artifacts], ["原始.markdown"])


class QuizPdfServerFontTests(unittest.TestCase):
    def test_quiz_pdf_embeds_its_cjk_font_when_the_machine_has_none(self):
        """A bare Linux server has no system CJK font; the PDF must still carry its own."""
        from services import task_pdf

        quiz = {"filename": "高三數學", "questions": [{
            "origin": "original", "stem": "已知複數 z = 3 − 4i，則 |z| 的值為", "type": "choice",
            "options": ["5", "7", "25", "−5"], "answer": "A", "explanation": "√(3²+4²)=5",
            "evidence": "原文", "page": 1}]}
        with tempfile.TemporaryDirectory() as folder, patch.object(task_pdf, "_FONT_CANDIDATES", ()):
            target = Path(folder) / "quiz.pdf"
            task_pdf.export_quiz_pdf(quiz, target, answers=True)
            with fitz.open(target) as doc:
                text = doc[0].get_text()
                fonts = doc[0].get_fonts()
        self.assertIn("已知複數", text)
        embedded = [font for font in fonts if font[4] == "quizfont"]
        self.assertTrue(embedded and all(font[1] != "n/a" for font in embedded), fonts)
        self.assertFalse(any("china" in str(font).lower() for font in fonts), fonts)


class LatexRejectionTests(unittest.TestCase):
    """Raw LaTeX in a Word/PDF shows up as garbled symbols, whichever tool wrote it."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.skills = FileSkills({}, Path(self.temporary.name))

    def test_every_writer_refuses_latex_but_accepts_unicode_math(self):
        latex = {"title": "練習卷", "paragraphs": [r"1. 複數 $z = \dfrac{3+4i}{1-i}$ 的代數形式為"]}
        for kind in ("pdf", "docx"):
            with self.assertRaisesRegex(FileSkillError, "LaTeX"):
                self.skills.create_file(kind, f"bad.{kind}", latex)
        with self.assertRaisesRegex(FileSkillError, "LaTeX"):
            self.skills.create_file("pptx", "bad.pptx", {"slides": [{"title": "t", "body": [r"$$x^2$$"]}]})
        good = {"title": "練習卷", "paragraphs": ["1. 複數 z = (3+4i)/(1−i) 的代數形式為", '檔案在 fixtures/synthetic-document.pdf']}
        made = self.skills.create_file("pdf", "good.pdf", good)
        self.assertTrue(Path(made["path"]).is_file())

    def test_editing_cannot_introduce_latex_either(self):
        made = self.skills.create_file("docx", "base.docx", {"title": "t", "paragraphs": ["一"]})
        with self.assertRaisesRegex(FileSkillError, "LaTeX"):
            self.skills.edit_file(made["file_id"], "edited.docx",
                                  [{"op": "set_paragraph", "index": 0, "text": r"$\sqrt{2}$"}])


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_title_and_wording_recall_a_file_that_meaning_alone_ranks_low(self):
        """A trip itinerary phrased differently from the question still reaches the evidence step."""
        index = FullTextIndex(self.root / "index")
        records = {}

        def encode(texts):
            # Everything except the itinerary looks semantically close to the question.
            return np.array([[0, 1] if "北京七天" in text else [1, 0] for text in texts], dtype=np.float32)

        with patch("services.fulltext._encode", side_effect=encode):
            for number in range(8):
                path = self.root / f"高三數學練習{number}.md"
                path.write_text(f"第 {number} 題 高三 考試 時間 計算", encoding="utf-8")
                records[path.stem] = {"id": path.stem, "name": path.name}
                index.ensure(records[path.stem], str(path), "")
            trip = self.root / "高三_毕业旅行行程表.md"
            trip.write_text("北京七天愛國之旅 11/24 出發 11/30 回程", encoding="utf-8")
            records["trip"] = {"id": "trip", "name": trip.name}
            index.ensure(records["trip"], str(trip), "")
            found = index.candidates(records, ["高三畢業旅行時間"], limit=4)
        self.assertIn("trip", {chunk["document_id"] for chunk in found})

    def test_index_survives_copying_the_file_to_another_folder(self):
        """Moving data between machines changes paths and mtimes, not what is indexed."""
        first = self.root / "a"
        second = self.root / "b"
        first.mkdir()
        second.mkdir()
        original = first / "行程.md"
        original.write_text("11/24 出發", encoding="utf-8")
        index = FullTextIndex(self.root / "index")
        record = {"id": "doc", "name": "行程.md"}
        calls = []

        def encode(texts):
            calls.append(list(texts))
            return np.ones((len(texts), 2), dtype=np.float32)

        with patch("services.fulltext._encode", side_effect=encode):
            self.assertEqual(index.ensure(record, str(original), ""), [])
            built = len(calls)
            copy = second / "行程.md"
            copy.write_bytes(original.read_bytes())
            self.assertEqual(index.ensure(record, str(copy), ""), [])
        self.assertGreater(built, 0)
        self.assertEqual(len(calls), built)


if __name__ == "__main__":
    unittest.main()
