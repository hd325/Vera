"""Offline API contracts for model-planned retrieval, scope and honest citations.

The model's decisions are explicit fixtures. Authorization, source scoping,
streaming, persistence, and the semantic retrieval boundary execute real code.
"""
from __future__ import annotations
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch
from fastapi import HTTPException
import tests
import main
from services.turn_planner import default_plan
from tests.test_web_search_api import WebSearchApiFixture

REAL_EVIDENCE = main._semantic_file_evidence

class AnswerRoutingApiTests(WebSearchApiFixture):
    def no_hits(self):
        self.retrieve.return_value = []
        self.plan.update(knowledge_scope="general", source="none")

    def test_general_question_probes_files_without_inventing_citations(self):
        self.no_hits()
        for route in self.routes:
            with self.subTest(route=route):
                self.reset_conversation()
                _, events = self.ask(route, question="解釋牛頓第二定律", search=False)
                self.assertEqual(events[-1]["type"], "done")
                self.assertEqual(events[-1]["citation_map"], [])
                self.assertEqual(self.latest_assistant()["referenced_files"], [])
                self.assertNotIn("LOCAL_FILE_BODY", self.prompts[-1]["prompt"])
        self.gather.assert_not_awaited()

    def test_public_question_can_use_web_without_local_evidence(self):
        self.no_hits()
        _, events = self.ask("/api/chat", search=True)
        self.assertEqual(events[-1]["web_sources"][0]["id"], "W1")
        self.assertEqual(events[-1]["citation_map"], [])
        self.assertIn("FETCHED_EVIDENCE_IN_PROMPT", self.prompts[-1]["prompt"])

    def test_school_plan_uses_local_evidence_on_both_routes(self):
        self.plan.update(source="search", knowledge_scope="school")
        self.answer = "本校安排見原文[1]。"
        for route in self.routes:
            with self.subTest(route=route):
                self.reset_conversation()
                _, events = self.ask(route, search=False)
                self.assertEqual(events[-1]["citation_map"], self.citations)
                self.assertIn("LOCAL_FILE_BODY", self.prompts[-1]["prompt"])
        self.gather.assert_not_awaited()

    def test_school_question_without_evidence_is_honest(self):
        self.no_hits()
        self.plan.update(source="search", knowledge_scope="school")
        _, events = self.ask("/api/chat", search=False)
        self.assertEqual(events[-1]["citation_map"], [])
        self.assertIn("請明確告知目前無法依據文件庫回答", self.prompts[-1]["prompt"])

    def test_authorized_explicit_file_reaches_semantic_boundary(self):
        with patch.object(main, "_semantic_file_evidence", AsyncMock(side_effect=self.evidence)) as evidence:
            self.ask("/api/chat", file_ids=["doc-fixture"], search=False)
        self.assertEqual(evidence.await_args.kwargs["file_ids"], ["doc-fixture"])

    def test_private_explicit_id_is_rejected_before_retrieval(self):
        self.store._files.append({"id":"hidden", "name":"secret.txt", "audience":"personal", "owner":"other"})
        with patch.object(main, "_semantic_file_evidence", AsyncMock()) as evidence:
            response = self.client.post("/api/chat", headers=self.headers, json={"session_id":"owner-session", "question":"閱讀它", "file_ids":["hidden"]})
        self.assertEqual(response.status_code, 403)
        evidence.assert_not_awaited()
        self.assertEqual(self.prompts, [])

    def test_pending_explicit_id_is_not_read_by_another_account(self):
        self.store._files.append({"id":"draft", "name":"draft.txt", "audience":"school", "owner":"other", "review_status":"pending"})
        response = self.client.post("/api/chat", headers=self.headers, json={"session_id":"owner-session", "question":"閱讀它", "file_ids":["draft"]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.prompts, [])

    def test_explicit_name_cannot_resolve_private_file(self):
        self.store._files.append({"id":"hidden", "name":"secret.txt", "audience":"personal", "owner":"other"})
        response = self.client.post("/api/chat", headers=self.headers, json={"session_id":"owner-session", "question":"閱讀它", "file_references":"secret.txt"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.prompts, [])

    def test_ambiguous_names_require_user_selection(self):
        self.store._files.append({"id":"duplicate", "name":"fixture.txt", "audience":"personal", "owner":"fixture-owner"})
        response = self.client.post("/api/chat", headers=self.headers, json={"session_id":"owner-session", "question":"閱讀它", "file_references":"fixture.txt"})
        self.assertEqual(response.status_code, 422)

    def test_previous_plan_inherits_only_authorized_saved_sources(self):
        self.plan.update(source="previous")
        self.store._conversations["fixture-conversation"]["messages"].append({"id":"previous", "is_user":False,
            "content":"文件說明[1]", "referenced_files":["doc-fixture"]})
        with patch.object(main, "_semantic_file_evidence", AsyncMock(side_effect=self.evidence)) as evidence:
            self.ask("/api/chat", search=False)
        self.assertEqual(evidence.await_args.kwargs["file_ids"], ["doc-fixture"])

    def test_new_topic_does_not_inherit_previous_file_scope(self):
        self.no_hits()
        self.store._conversations["fixture-conversation"]["messages"].append({"id":"previous", "is_user":False,
            "content":"文件說明[1]", "referenced_files":["doc-fixture"]})
        with patch.object(main, "_semantic_file_evidence", AsyncMock(side_effect=self.evidence)) as evidence:
            self.ask("/api/chat", search=False)
        self.assertEqual(evidence.await_args.kwargs["file_ids"], [])
        self.assertNotIn("LOCAL_FILE_BODY", self.prompts[-1]["prompt"])

    def test_foreign_conversation_history_never_enters_prompt(self):
        self.no_hits()
        foreign = self.store._conversations["fixture-conversation"]
        foreign.update(user_id="other-owner", messages=[{"is_user":False,"content":"FOREIGN_PRIVATE_HISTORY"}])
        before = copy.deepcopy(foreign)
        self.ask("/api/chat", search=False)
        self.assertEqual(foreign, before)
        self.assertNotIn("FOREIGN_PRIVATE_HISTORY", self.prompts[-1]["prompt"])

    def test_thought_and_code_citations_do_not_become_saved_sources(self):
        self.answer = "<think>候選[1]</think>示例：`[1]`\n```text\n[1]\n```\n目前尚無可確認結論。"
        self.ask("/api/chat", file_ids=["doc-fixture"], search=False)
        self.assertEqual(self.latest_assistant()["referenced_files"], [])
        self.assertEqual(self.latest_assistant()["citation_map"], self.citations)

    def test_retrieval_status_and_citations_survive_reload(self):
        _, events = self.ask("/api/chat", search=False)
        saved = self.latest_assistant(main.DataStore())
        self.assertEqual(saved["citation_map"], events[-1]["citation_map"])
        self.assertEqual(saved["rag_status"], events[-1]["rag_status"])

    def test_failed_model_planning_cannot_execute_referenced_file_task(self):
        with patch.object(main, "plan_turn", AsyncMock(return_value=default_plan())):
            response = self.client.post("/api/chat", headers=self.headers, json={"session_id":"owner-session", "question":"處理它", "file_ids":["doc-fixture"]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.prompts, [])

    def test_semantic_boundary_filters_private_pending_and_deleted_records(self):
        self.store._files.extend([
            {"id":"hidden", "name":"hidden.txt", "audience":"personal", "owner":"other"},
            {"id":"draft", "name":"draft.txt", "audience":"school", "owner":"other", "review_status":"pending"},
            {"id":"gone", "name":"gone.txt", "audience":"school", "review_status":"published"}])
        self.store._deleted_files["gone"] = {"deleted_at":"synthetic"}
        index = Mock()
        index.ensure.return_value = []
        seen = []
        def candidates(records, queries):
            seen.append(set(records))
            return copy.deepcopy(self.hits)
        index.candidates.side_effect = candidates
        with patch.object(main, "_fulltext_index", return_value=index), patch.object(main, "_build_indexed_catalog_entries", return_value=[]), \
             patch("services.turn_planner.select_evidence", AsyncMock(side_effect=lambda config,q,c,**kw:(c,""))):
            chunks, status = asyncio.run(REAL_EVIDENCE("問題", "fixture-owner", self.plan))
        self.assertEqual(seen, [{"doc-fixture"}])
        self.assertEqual([c["document_id"] for c in chunks], ["doc-fixture"])

    def test_permission_is_rechecked_after_model_evidence_selection(self):
        index = Mock()
        index.ensure.return_value = []
        index.candidates.return_value = copy.deepcopy(self.hits)
        async def revoke(config, question, candidates, **kwargs):
            self.store._files[0]["owner"] = "other"
            return candidates, ""
        with patch.object(main, "_fulltext_index", return_value=index), patch.object(main, "_build_indexed_catalog_entries", return_value=[]), \
             patch("services.turn_planner.select_evidence", AsyncMock(side_effect=revoke)):
            chunks, status = asyncio.run(REAL_EVIDENCE("問題", "fixture-owner", self.plan))
        self.assertEqual(chunks, [])
        self.assertEqual(status["accepted_count"], 0)

if __name__ == "__main__": unittest.main()
