"""Real streaming transport with offline model doubles and actual message writes."""

import asyncio
import json
import unittest
import uuid

from sqlalchemy import select

from api.chat_stream import ChatRequest, persisted_chat_response
from api.models import ChatLog, ChatMessage
import test_chat_storage as storage_tests


class ChatPersistenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = storage_tests.ChatStorageTests.asyncSetUp
    asyncTearDown = storage_tests.ChatStorageTests.asyncTearDown
    begin = storage_tests.ChatStorageTests.begin

    async def body(self, response):
        return "".join([frame async for frame in response.body_iterator])

    def response(self, turn, model):
        return persisted_chat_response(turn, object(), model, lambda _: {"available": False})

    async def test_commits_final_reply_before_done_and_uses_owned_context(self):
        turn = await self.begin()
        calls = []

        async def model(**kwargs):
            calls.append(kwargs)
            self.assertEqual(turn.assistant.status, "streaming")
            await kwargs["on_token"]("答")
            return "done", "完整回答"

        response = self.response(turn, model)
        frames = []
        async for frame in response.body_iterator:
            frames.append(frame)
            if frame.startswith("event: done"):
                async with self.factory() as reader:
                    row = await reader.get(ChatMessage, turn.assistant_id)
                    self.assertEqual((row.status, row.content), ("completed", "完整回答"))
                    self.assertIsNotNone(await reader.get(ChatLog, turn.assistant_id))
        body = "".join(frames)
        self.assertIn("event: token", body)
        self.assertIn("event: usage", body)
        self.assertIn(str(turn.assistant_id), body)
        self.assertEqual(calls[0]["user_id"], str(self.owner))
        self.assertEqual(calls[0]["session_id"], str(self.session.id))

    async def test_provider_failure_saves_partial_without_success_log_then_new_request_can_retry(self):
        turn = await self.begin()

        async def failing(**kwargs):
            await kwargs["on_token"]("部分内容")
            raise RuntimeError("simulated provider failure")

        with self.assertLogs("api.chat_stream", level="ERROR"):
            body = await self.body(self.response(turn, failing))
        self.assertIn("event: error", body)
        self.assertNotIn("event: done", body)
        await self.db.refresh(turn.assistant)
        self.assertEqual((turn.assistant.status, turn.assistant.content), ("failed", "部分内容"))
        self.assertEqual((await self.db.scalars(select(ChatLog))).all(), [])
        retry = await self.begin()

        async def succeeding(**_kwargs):
            return "done", "重试成功"

        self.assertIn("event: done", await self.body(self.response(retry, succeeding)))
        self.assertEqual(len((await self.db.scalars(select(ChatLog))).all()), 1)

    async def test_replay_never_calls_model_or_invents_token_usage(self):
        request_id = uuid.uuid4()
        turn = await self.begin(request_id)
        await turn.finish("completed", "原回复")
        replay = await self.begin(request_id)

        async def forbidden(**_kwargs):
            raise AssertionError("duplicate model invocation")

        body = await self.body(self.response(replay, forbidden))
        self.assertIn('"replayed": true', body)
        self.assertIn("原回复", body)
        self.assertNotIn("event: usage", body)

    async def test_memory_failure_is_persisted_and_replayed_without_rerunning_model(self):
        request_id = uuid.uuid4()
        turn = await self.begin(request_id, message="我叫张三")

        async def model(**_kwargs):
            return "done", "你好，张三", "failed"

        first = await self.body(self.response(turn, model))
        self.assertIn('"memory_status": "failed"', first)
        self.assertIn("event: done", first)
        async with self.factory() as reader:
            saved = await reader.get(ChatMessage, turn.assistant_id)
            self.assertEqual(saved.memory_status, "failed")

        replay = await self.begin(request_id, message="我叫张三")

        async def forbidden(**_kwargs):
            raise AssertionError("duplicate model invocation")

        second = await self.body(self.response(replay, forbidden))
        self.assertIn('"memory_status": "failed"', second)
        self.assertIn('"replayed": true', second)

    async def test_stopped_reply_keeps_partial_and_emits_stopped(self):
        turn = await self.begin()

        async def stopped(**kwargs):
            await kwargs["on_token"]("部分内容")
            return "stopped", "部分内容"

        body = await self.body(self.response(turn, stopped))
        self.assertIn("event: stopped", body)
        self.assertNotIn("event: done", body)
        await self.db.refresh(turn.assistant)
        self.assertEqual((turn.assistant.status, turn.assistant.content), ("stopped", "部分内容"))

    async def test_save_failure_never_emits_done_and_marks_unknown_checkpoint(self):
        turn = await self.begin()
        finish = turn.finish

        async def failing_finish(status, content, error_code=None):
            if status == "completed":
                raise RuntimeError("simulated final commit failure")
            return await finish(status, content, error_code)

        turn.finish = failing_finish

        async def model(**_kwargs):
            return "done", "已经生成的回答"

        with self.assertLogs("api.chat_stream", level="ERROR"):
            body = await self.body(self.response(turn, model))
        self.assertIn("event: error", body)
        self.assertNotIn("event: done", body)
        await self.db.refresh(turn.assistant)
        self.assertEqual(turn.assistant.status, "interrupted")
        self.assertEqual(turn.assistant.error_code, "CHECKPOINT_RECOVERY_REQUIRED")

    async def test_disconnect_cancels_model_and_saves_its_stopped_result(self):
        turn = await self.begin()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def model(**kwargs):
            await kwargs["on_token"]("部分内容")
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                return "stopped", "部分内容"

        iterator = self.response(turn, model).body_iterator
        await anext(iterator)  # connected comment; model is now scheduled
        await asyncio.wait_for(started.wait(), 2)
        self.assertIn("event: token", await anext(iterator))
        await iterator.aclose()
        self.assertTrue(cancelled.is_set())
        await self.db.refresh(turn.assistant)
        self.assertEqual(turn.assistant.status, "stopped")

    async def test_input_rejects_missing_request_id_and_blank_message(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            ChatRequest(session_id=uuid.uuid4(), message="hello")
        with self.assertRaises(ValidationError):
            ChatRequest(session_id=uuid.uuid4(), request_id=uuid.uuid4(), message="  ")

    async def test_upstream_cancellation_emits_error_instead_of_hanging(self):
        turn = await self.begin()

        async def cancelled(**_kwargs):
            raise asyncio.CancelledError()

        body = await asyncio.wait_for(self.body(self.response(turn, cancelled)), 2)
        self.assertIn("event: error", body)
        await self.db.refresh(turn.assistant)
        self.assertEqual(turn.assistant.status, "interrupted")


if __name__ == "__main__":
    unittest.main()
