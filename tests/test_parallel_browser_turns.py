import asyncio
import json
import unittest
import uuid
from types import SimpleNamespace

from fastapi.responses import StreamingResponse

import tests  # Establish synthetic-only storage before application imports.
from main import QuestionRequest
from services.browser_turn import BrowserTurn, active_turns, browser_turn_response, cancel_saved_file, control_turn, event
from services.channel_chat import ConversationScheduler


class ConversationStore:
    def __init__(self):
        self.messages = {str(number): [] for number in range(4)}

    async def get_session(self, identifier):
        return {"account": "owner"} if identifier == "session" else None

    async def get_conversation(self, identifier):
        return {"user_id": "owner", "messages": self.messages[identifier]}

    async def append_owned_messages(self, owner, cid, messages):
        assert owner == "owner"
        self.messages[cid].extend(messages)


class ParallelBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_supplement_is_rejected_before_model_and_fresh_message_is_accepted(self):
        store = ConversationStore()
        host = SimpleNamespace(data_store=store)
        scheduler = ConversationScheduler()
        handled = []
        request = QuestionRequest(question="整理成 PDF", session_id="session", conversation_id="0", turn_id=uuid.uuid4())
        waiting = BrowserTurn(host, request, "session", "owner", "0")
        waiting.kind = "file"
        waiting.clarify("請問是哪一科？")
        await waiting.publish_pause()
        await cancel_saved_file(host, "owner", "0", waiting.reply_id)

        async def handler(current, session_id):
            handled.append(current.question)
            return StreamingResponse(iter([event({"type": "done"})]), media_type="text/event-stream")

        for anchor in (waiting.reply_id, None):
            current = QuestionRequest(question="英文", session_id="session", conversation_id="0",
                                      turn_id=uuid.uuid4(), clarification_reply_to=anchor)
            response = browser_turn_response(host, scheduler, handler, current, "session", "owner", "0")
            events = [json.loads(chunk[6:]) async for chunk in response.body_iterator]
            if anchor:
                self.assertEqual(events[-1]["code"], "turn_unavailable")
                self.assertEqual(handled, [])
            else:
                self.assertEqual(handled, ["英文"])
                self.assertEqual(events[-1]["type"], "done")

    async def test_saved_clarification_retains_original_request_across_hidden_message(self):
        store = ConversationStore()
        host = SimpleNamespace(data_store=store)
        request = QuestionRequest(question="整理 10 個單詞成 PDF", session_id="session", conversation_id="0",
                                  turn_id=uuid.uuid4())
        waiting = BrowserTurn(host, request, "session", "owner", "0")
        waiting.clarify("請問是哪一科？")
        await waiting.publish_pause()
        store.messages["0"].append({"is_user": False, "channel_replay_invalidated": True})
        current = QuestionRequest(question="高三英文", session_id="session", conversation_id="0",
                                  turn_id=uuid.uuid4(), clarification_reply_to=waiting.reply_id)
        continued = BrowserTurn(host, current, "session", "owner", "0")
        await continued.prepare()
        self.assertEqual(continued.request.question, "整理 10 個單詞成 PDF")
        self.assertIn("高三英文", continued.context)
        self.assertEqual(continued.parent_metadata(), {"_web_parent": waiting.reply_id})

    async def test_disconnect_does_not_pause_and_stop_affects_only_one_conversation(self):
        store = ConversationStore()
        host = SimpleNamespace(data_store=store)
        scheduler = ConversationScheduler()
        all_streaming = asyncio.Event()
        release = asyncio.Event()
        streaming = set()

        async def handler(request, session_id):
            turn = request._web_turn

            async def generate():
                yield event({"type": "content", "text": "開頭"})
                streaming.add(turn.cid)
                if len(streaming) == 4:
                    all_streaming.set()
                await release.wait()
                turn.begin_commit()
                await store.append_owned_messages(turn.owner, turn.cid, [
                    {"id": turn.reply_id, "is_user": False, "content": f"開頭{turn.cid}"}
                ])
                yield event({"type": "done", "message_id": turn.reply_id})

            return StreamingResponse(generate(), media_type="text/event-stream")

        subscriptions = []
        turns = []
        try:
            for cid in store.messages:
                request = QuestionRequest(question=cid, session_id="session", conversation_id=cid, turn_id=uuid.uuid4())
                response = browser_turn_response(host, scheduler, handler, request, "session", "owner", cid)
                subscriber = response.body_iterator
                subscriptions.append(subscriber)
                first = json.loads((await anext(subscriber))[6:])
                turn = active_turns[(id(store), str(request.turn_id))]
                turns.append(turn)
                self.assertEqual((first["conversation_id"], first["reply_id"]), (cid, turn.reply_id))
            await asyncio.wait_for(all_streaming.wait(), 3)

            await subscriptions[0].aclose()
            stopped = turns[1]
            result = await control_turn(host, "session", stopped.id, "pause", 2, stopped.cid)
            self.assertEqual(result["status"], "stopping")
            await asyncio.wait_for(stopped.work, 3)
            self.assertIsNone(turns[0].action)
            self.assertTrue(all(turn.action is None for turn in turns[2:]))

            release.set()
            await asyncio.wait_for(asyncio.gather(*(turn.work for turn in turns)), 3)
            for cid in ("0", "2", "3"):
                self.assertEqual(store.messages[cid][0]["content"], f"開頭{cid}")
                self.assertNotIn("turn_control", store.messages[cid][0])
            self.assertEqual(store.messages["1"][-1]["turn_control"]["status"], "stopped")
            self.assertEqual(store.messages["1"][-1]["content"], "開頭")
            self.assertFalse(scheduler.lanes)
        finally:
            release.set()
            for subscription in subscriptions:
                await subscription.aclose()
            await asyncio.gather(*(turn.work for turn in turns), return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
