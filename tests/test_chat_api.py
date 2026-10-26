"""HTTP schema, dependency lifetime, ownership and request-id regressions."""

import asyncio
import unittest
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from api.models import ChatMessage, ChatSession
from chat_api_fixture import MemoryLockPool, make_app
import test_chat_storage as fixtures


class ChatApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ChatStorageTests.asyncSetUp
    asyncTearDown = fixtures.ChatStorageTests.asyncTearDown

    async def test_create_validation_authorization_and_replay_over_http(self):
        calls = []
        pool = MemoryLockPool()

        async def model(**kwargs):
            calls.append(kwargs)
            self.assertTrue(pool.keys)  # Lock still held after message reservation committed.
            async with self.factory() as db:
                rows = (await db.scalars(select(ChatMessage))).all()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[1].status, "streaming")
            await kwargs["on_token"]("回复")
            return "done", "完整回复"

        app = make_app(self.factory, pool, model)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            self.assertEqual((await client.post("/api/sessions", json={})).status_code, 401)
            headers = {"x-test-user": str(self.owner)}
            self.assertEqual((await client.post("/api/sessions", headers=headers, json={"owner_id": str(self.other)})).status_code, 422)
            created = await client.post("/api/sessions", headers=headers, json={"title": "测试"})
            self.assertEqual(created.status_code, 201)
            sid = created.json()["data"]["id"]
            request = {"session_id": sid, "request_id": str(uuid.uuid4()), "message": "你好"}
            forbidden = await client.post("/chat", headers={"x-test-user": str(self.other)}, json=request)
            self.assertEqual(forbidden.status_code, 404)
            self.assertEqual(calls, [])
            invalid = dict(request); invalid.pop("request_id")
            self.assertEqual((await client.post("/chat", headers=headers, json=invalid)).status_code, 422)
            result = await client.post("/chat", headers=headers, json=request)
            self.assertEqual(result.status_code, 200)
            self.assertIn("event: done", result.text)
            self.assertEqual(pool.keys, set())
            replay = await client.post("/chat", headers=headers, json=request)
            self.assertIn('"replayed": true', replay.text)
            self.assertEqual(len(calls), 1)
            request["message"] = "不同正文"
            self.assertEqual((await client.post("/chat", headers=headers, json=request)).status_code, 409)
            self.assertEqual(pool.keys, set())
            self.assertEqual(pool.borrowed, 0)

    async def test_overlapping_requests_are_rejected_and_lock_released_after_stream(self):
        started = asyncio.Event()
        finish = asyncio.Event()
        calls = 0
        pool = MemoryLockPool()

        async def model(**kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await finish.wait()
            return "done", "回答"

        app = make_app(self.factory, pool, model)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = {"x-test-user": str(self.owner)}
            request = {"session_id": str(self.session.id), "request_id": str(uuid.uuid4()), "message": "问题"}
            active = asyncio.create_task(client.post("/chat", headers=headers, json=request))
            try:
                await asyncio.wait_for(started.wait(), 2)
                duplicate = await client.post("/chat", headers=headers, json=request)
                self.assertEqual(duplicate.status_code, 409)
                self.assertEqual(calls, 1)
            finally:
                finish.set()
                result = await asyncio.wait_for(active, 2)
            self.assertIn("event: done", result.text)
            self.assertEqual(pool.keys, set())
            request["request_id"] = str(uuid.uuid4())
            self.assertEqual((await client.post("/chat", headers=headers, json=request)).status_code, 200)
            self.assertEqual(calls, 2)
            self.assertEqual(pool.keys, set())
            self.assertEqual(pool.borrowed, 0)

    async def test_http_disconnect_releases_lock_after_stopped_result_is_saved(self):
        started = asyncio.Event()
        pool = MemoryLockPool()

        async def model(**kwargs):
            await kwargs["on_token"]("部分内容")
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return "stopped", "部分内容"

        app = make_app(self.factory, pool, model)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            active = asyncio.create_task(client.post(
                "/chat", headers={"x-test-user": str(self.owner)},
                json={"session_id": str(self.session.id), "request_id": str(uuid.uuid4()), "message": "问题"},
            ))
            await asyncio.wait_for(started.wait(), 2)
            active.cancel()
            try:
                await asyncio.wait_for(active, 2)
            except asyncio.CancelledError:
                pass
        self.assertEqual(pool.keys, set())
        self.assertEqual(pool.borrowed, 0)
        async with self.factory() as db:
            row = await db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
            self.assertEqual((row.status, row.content), ("stopped", "部分内容"))


class LockCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def check_broken_connection(self, fail_fetch):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from api.chat_storage import session_execution_lock

        events = []

        class Connection:
            async def execute(self, statement, _params):
                if "pg_try_advisory_lock" in statement:
                    async def fetchone():
                        if fail_fetch:
                            raise OSError("lost acquisition response")
                        return {"acquired": True}
                    return SimpleNamespace(fetchone=fetchone)
                raise OSError("lost unlock response")

            async def close(self):
                events.append("closed")

        @asynccontextmanager
        async def connection():
            try:
                yield Connection()
            finally:
                events.append("returned")

        with self.assertRaises(OSError):
            async with session_execution_lock(SimpleNamespace(connection=connection), uuid.uuid4(), uuid.uuid4()):
                events.append("executed")
        self.assertEqual(events[-2:], ["closed", "returned"])

    async def test_lost_acquire_response_closes_connection_before_pool_return(self):
        await self.check_broken_connection(True)

    async def test_unlock_failure_closes_connection_before_pool_return(self):
        await self.check_broken_connection(False)


if __name__ == "__main__":
    unittest.main()
