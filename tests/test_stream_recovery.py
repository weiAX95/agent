"""Offline regressions for streamed generation failures and checkpoint recovery."""

import ast
import asyncio
import copy
import json
import logging
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage
from starlette.responses import StreamingResponse


ROOT = Path(__file__).resolve().parents[1]


def load_function(path, name, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    function = copy.deepcopy(function)
    function.decorator_list = []
    module = ast.fix_missing_locations(
        ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__",
                    names=[ast.alias(name="annotations")],
                    level=0,
                ),
                function,
            ],
            type_ignores=[],
        )
    )
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


class CheckpointAgent:
    def __init__(self, fail_first=True):
        self.messages = [
            HumanMessage(id="old-user", content="之前的问题"),
            AIMessage(id="old-ai", content="之前的完整回答"),
        ]
        self.calls = 0
        self.state_updates = []
        self.fail_first = fail_first

    async def astream(self, payload, **kwargs):
        self.calls += 1
        user_message = payload["messages"][0]
        self.messages.append(user_message)

        if self.fail_first and self.calls == 1:
            yield {
                "type": "messages",
                "data": (AIMessageChunk(content="已经生成"), {"langgraph_node": "model"}),
            }
            yield {
                "type": "messages",
                "data": (AIMessageChunk(content="一部分"), {"langgraph_node": "model"}),
            }
            self.messages.append(AIMessage(id="partial-ai", content="已经生成一部分"))
            raise RuntimeError("simulated provider stream failure")

        answer = "恢复后的完整回答"
        self.messages.append(AIMessage(id=f"answer-{self.calls}", content=answer))
        yield {
            "type": "messages",
            "data": (AIMessageChunk(content=answer), {"langgraph_node": "model"}),
        }
        yield {
            "type": "updates",
            "data": {"model": {"messages": [AIMessage(content=answer)]}},
        }

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": list(self.messages)})

    async def aupdate_state(self, config, update):
        self.state_updates.append(update)
        removals = {
            item.id for item in update["messages"] if isinstance(item, RemoveMessage)
        }
        self.messages = [item for item in self.messages if item.id not in removals]


class StreamRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def response_text(self, response):
        frames = []
        async for frame in response.body_iterator:
            frames.append(frame.decode() if isinstance(frame, bytes) else frame)
        return "".join(frames)

    def make_streamer(self, agent, *, memory_error=False):
        memory_updates = []

        def prepare(message, session_id, user_id, current_agent, callbacks=None):
            return (
                {},
                lambda keys: [],
                {"messages": []},
                {"configurable": {"thread_id": f"{user_id}:{session_id}"}},
                {"system_prompt": "test"},
            )

        def update_memory(*args):
            memory_updates.append(args)
            if memory_error:
                raise RuntimeError("simulated memory update failure")

        namespace = {
            "asyncio": asyncio,
            "uuid": uuid,
            "HumanMessage": HumanMessage,
            "AIMessage": AIMessage,
            "RemoveMessage": RemoveMessage,
            "_prepare_chat": prepare,
            "_update_long_term_memory": update_memory,
        }
        load_function(ROOT / "src/agent/main.py", "_content_to_text", namespace)
        load_function(ROOT / "src/agent/main.py", "_discard_incomplete_turn", namespace)
        streamer = load_function(ROOT / "src/agent/main.py", "chat_stream", namespace)
        return streamer, memory_updates

    async def test_partial_model_failure_cleans_only_current_turn_then_same_thread_recovers(self):
        agent = CheckpointAgent()
        streamer, memory_updates = self.make_streamer(agent)
        tokens = []

        async def on_token(token):
            tokens.append(token)

        with self.assertRaisesRegex(RuntimeError, "simulated provider stream failure"):
            await streamer(
                "新问题",
                "session-1",
                "user-1",
                agent,
                on_token,
            )

        self.assertEqual(tokens, ["已经生成", "一部分"])
        self.assertEqual(
            [(item.type, item.content) for item in agent.messages],
            [("human", "之前的问题"), ("ai", "之前的完整回答")],
        )
        self.assertEqual(len(agent.state_updates), 1)
        self.assertEqual(memory_updates, [])

        status, answer = await streamer(
            "同会话重试",
            "session-1",
            "user-1",
            agent,
            on_token,
        )
        self.assertEqual((status, answer), ("done", "恢复后的完整回答"))
        self.assertEqual(
            [(item.type, item.content) for item in agent.messages],
            [
                ("human", "之前的问题"),
                ("ai", "之前的完整回答"),
                ("human", "同会话重试"),
                ("ai", "恢复后的完整回答"),
            ],
        )
        self.assertEqual(len(memory_updates), 1)

    async def test_memory_failure_after_complete_answer_does_not_delete_checkpoint(self):
        agent = CheckpointAgent(fail_first=False)
        streamer, memory_updates = self.make_streamer(agent, memory_error=True)

        async def ignore_token(token):
            return None

        with self.assertRaisesRegex(RuntimeError, "simulated memory update failure"):
            await streamer("新问题", "session-2", "user-1", agent, ignore_token)

        self.assertEqual(
            [(item.type, item.content) for item in agent.messages],
            [
                ("human", "之前的问题"),
                ("ai", "之前的完整回答"),
                ("human", "新问题"),
                ("ai", "恢复后的完整回答"),
            ],
        )
        self.assertEqual(agent.state_updates, [])
        self.assertEqual(len(memory_updates), 1)

    async def test_api_sends_sse_error_without_chatlog_then_retry_commits(self):
        calls = 0
        chatlog_rows = []

        async def fake_chat_stream(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                await kwargs["on_token"]("部分 token")
                raise RuntimeError("simulated stream failure")
            return "done", "重试成功"

        class FakeUsage:
            def __init__(self):
                self.usage_metadata = {}

        class FakeDB:
            def __init__(self):
                self.added = []
                self.commits = 0
                self.rollbacks = 0

            async def execute(self, statement, params):
                return None

            def add(self, row):
                self.added.append(row)
                chatlog_rows.append(row)

            async def commit(self):
                self.commits += 1

            async def rollback(self):
                self.rollbacks += 1

        namespace = {
            "asyncio": asyncio,
            "json": json,
            "sys": __import__("sys"),
            "logger": logging.getLogger("stream-recovery-test"),
            "chat_stream": fake_chat_stream,
            "UsageMetadataCallbackHandler": FakeUsage,
            "_usage_payload": lambda callback: {"available": False},
            "StreamingResponse": StreamingResponse,
            "Depends": lambda dependency: None,
            "get_db": lambda: None,
            "ChatLog": lambda **fields: SimpleNamespace(**fields),
            "text": lambda value: value,
            "checkpoint_lock_id": lambda user_id, session_id: 7,
        }
        endpoint = load_function(ROOT / "api/server.py", "chat_api", namespace)
        app_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent=object())))
        request = SimpleNamespace(message="问题", user_id="user-1", session_id="session-3")

        first_db = FakeDB()
        first_response = await endpoint(request, app_request, first_db)
        self.assertIsInstance(first_response, StreamingResponse)
        first_body = await self.response_text(first_response)
        self.assertIn("event: token", first_body)
        self.assertIn("event: error", first_body)
        self.assertEqual(first_db.added, [])
        self.assertEqual(first_db.commits, 0)
        self.assertEqual(first_db.rollbacks, 1)
        self.assertEqual(chatlog_rows, [])

        retry_db = FakeDB()
        retry_response = await endpoint(request, app_request, retry_db)
        retry_body = await self.response_text(retry_response)
        self.assertIn("event: done", retry_body)
        self.assertEqual(len(retry_db.added), 1)
        self.assertEqual(retry_db.added[0].answer, "重试成功")
        self.assertEqual(retry_db.commits, 1)
        self.assertEqual(len(chatlog_rows), 1)


if __name__ == "__main__":
    unittest.main()
