"""验证每次 HTTP 发送都有独立记录，并覆盖同步、异步与流式取消。"""

import unittest
import uuid

import httpx
from langchain_openai import ChatOpenAI

from src.agent.model_gateway import ModelGatewayConfig, build_model
from src.agent.physical_transport import (
    AsyncMeteredClient, MeteredClient, MeteredRun, active_metered_run,
)


class Sink:
    def __init__(self):
        self.started = []
        self.finished = []

    def start(self, run, provider, model):
        call_id = uuid.uuid4()
        self.started.append((call_id, run.run_id, provider, model))
        return call_id

    def finish(self, call_id, status):
        self.finished.append((call_id, status))

    async def astart(self, run, provider, model):
        return self.start(run, provider, model)

    async def afinish(self, call_id, status):
        self.finish(call_id, status)


class PhysicalTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sink = Sink()
        self.run_token = active_metered_run.set(MeteredRun(uuid.uuid4(), uuid.uuid4()))

    def tearDown(self):
        active_metered_run.reset(self.run_token)

    def test_sync_retries_are_separate_physical_calls(self):
        attempts = 0

        def upstream(request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(429 if attempts == 1 else 200,
                                  stream=httpx.ByteStream(b"{}"))

        with MeteredClient(self.sink, "openai", "test-model",
                           transport=httpx.MockTransport(upstream)) as client:
            self.assertEqual(client.post("https://example.test/v1/chat/completions").status_code, 429)
            self.assertEqual(client.post("https://example.test/v1/chat/completions").status_code, 200)
            client.get("https://example.test/health")
        self.assertEqual(len(self.sink.started), 2)
        self.assertNotEqual(self.sink.started[0][0], self.sink.started[1][0])
        self.assertEqual([status for _, status in self.sink.finished], ["failed", "succeeded"])

    async def test_gateway_model_uses_metered_clients(self):
        model = build_model(ModelGatewayConfig(
            "default", "test-model", "https://example.test/v1", "test", 5, 2,
        ))
        self.assertIsInstance(model.root_client._client, MeteredClient)
        self.assertIsInstance(model.root_async_client._client, AsyncMeteredClient)
        self.assertEqual(model.max_retries, 2)
        model.root_client._client.close()
        await model.root_async_client._client.aclose()

    def test_openai_sdk_retry_uses_two_call_ids(self):
        attempts = 0

        def upstream(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, json={"error": {"message": "busy", "type": "rate_limit_error"}})
            return httpx.Response(200, json={
                "id": "chatcmpl-test", "object": "chat.completion", "model": "test-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            })

        client = MeteredClient(self.sink, "openai", "test-model",
                               transport=httpx.MockTransport(upstream))
        model = ChatOpenAI(model="test-model", api_key="test", base_url="https://example.test/v1",
                           max_retries=1, http_client=client)
        try:
            self.assertEqual(model.invoke("hello").content, "ok")
        finally:
            client.close()
        self.assertEqual(attempts, 2)
        self.assertEqual(len({call_id for call_id, *_ in self.sink.started}), 2)
        self.assertEqual([status for _, status in self.sink.finished], ["failed", "succeeded"])

    async def test_async_timeout_and_success_have_distinct_records(self):
        attempts = 0

        def upstream(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ReadTimeout("timeout")
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

        async with AsyncMeteredClient(self.sink, "openai", "test-model",
                                      transport=httpx.MockTransport(upstream)) as client:
            with self.assertRaises(httpx.ReadTimeout):
                await client.post("https://example.test/v1/responses")
            self.assertEqual((await client.post("https://example.test/v1/responses")).status_code,
                             200)
        self.assertEqual([status for _, status in self.sink.finished], ["timed_out", "succeeded"])

    async def test_async_stream_early_close_is_cancelled(self):
        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"data: one\n\n"
                yield b"data: two\n\n"

            async def aclose(self):
                pass

        async with AsyncMeteredClient(
            self.sink, "openai", "test-model",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=Chunks())),
        ) as client:
            async with client.stream("POST", "https://example.test/v1/chat/completions") as response:
                async for _ in response.aiter_bytes():
                    break
        self.assertEqual([status for _, status in self.sink.finished], ["cancelled"])


if __name__ == "__main__":
    unittest.main()
