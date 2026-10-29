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
from chat_graph_loader import attach_chat_workflow


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

        def judge_memory(message, callbacks=None):
            memory_updates.append(message)
            if memory_error:
                raise RuntimeError("simulated memory update failure")
            return SimpleNamespace(memories=[])

        namespace = {
            "asyncio": asyncio,
            "uuid": uuid,
            "HumanMessage": HumanMessage,
            "AIMessage": AIMessage,
            "RemoveMessage": RemoveMessage,
            "_prepare_chat": prepare,
            "should_extract_memory": lambda message: True,
            "judge_memory": judge_memory,
        }
        load_function(ROOT / "src/agent/main.py", "_content_to_text", namespace)
        load_function(ROOT / "src/agent/main.py", "_restore_stopped_turn", namespace)
        load_function(ROOT / "src/agent/main.py", "_discard_incomplete_turn", namespace)
        attach_chat_workflow(namespace)
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

        status, answer, memory_status = await streamer(
            "同会话重试",
            "session-1",
            "user-1",
            agent,
            on_token,
        )
        self.assertEqual((status, answer), ("done", "恢复后的完整回答"))
        self.assertEqual(memory_status, "skipped")
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

        with self.assertLogs("src.agent.chat_workflow", level="ERROR"):
            self.assertEqual(
                await streamer("新问题", "session-2", "user-1", agent, ignore_token),
                ("done", "恢复后的完整回答", "failed"),
            )

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
        from api.chat_storage import begin_turn
        from api.chat_stream import persisted_chat_response
        from api.models import ChatLog, ChatMessage
        from sqlalchemy import select
        from test_chat_storage import ChatStorageTests

        await ChatStorageTests.asyncSetUp(self)
        calls = 0

        async def model(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                await kwargs["on_token"]("部分 token")
                raise RuntimeError("simulated stream failure")
            return "done", "重试成功"

        try:
            turn = await begin_turn(self.db, self.owner, self.session.id, uuid.uuid4(), "问题")
            with self.assertLogs("api.chat_stream", level="ERROR"):
                body = await self.response_text(persisted_chat_response(turn, object(), model, lambda _: {"available": False}))
            self.assertIn("event: token", body)
            self.assertIn("event: error", body)
            self.assertNotIn("event: done", body)
            self.assertEqual((await self.db.scalars(select(ChatLog))).all(), [])
            await self.db.refresh(turn.assistant)
            self.assertEqual(turn.assistant.status, "failed")
            retry = await begin_turn(self.db, self.owner, self.session.id, uuid.uuid4(), "问题")
            body = await self.response_text(persisted_chat_response(retry, object(), model, lambda _: {"available": False}))
            self.assertIn("event: done", body)
            logs = (await self.db.scalars(select(ChatLog))).all()
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].answer, "重试成功")
        finally:
            await ChatStorageTests.asyncTearDown(self)


if __name__ == "__main__":
    unittest.main()
