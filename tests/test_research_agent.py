"""The research worker receives only a scoped query and no chat checkpoint."""

import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch

from langgraph.errors import GraphRecursionError

from src.agent.research_agent import build_research_tool, research_result


class FakeWorker:
    def __init__(self):
        self.calls = []

    def invoke(self, payload, config=None):
        self.calls.append((payload, config))
        return {"messages": [SimpleNamespace(content="资料摘要及来源")]}

    async def ainvoke(self, payload, config=None):
        return self.invoke(payload, config)


class ResearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_limit_is_scoped_to_research_and_hidden_from_user_summary(self):
        result = {"messages": [
            SimpleNamespace(content="", tool_calls=[{"id": "one", "name": "search_web", "args": {"query": "x"}}]),
            SimpleNamespace(content="URL：https://example.com", tool_call_id="one", status="success"),
            SimpleNamespace(content="", tool_calls=[{"id": "two", "name": "search_web", "args": {"query": "y"}}]),
            SimpleNamespace(content="Tool call limit exceeded. Do not make additional tool calls.", tool_call_id="two", status="error"),
            SimpleNamespace(content="Tool call limit reached (5/4 calls)", tool_calls=[]),
        ]}
        data = json.loads(research_result(result))
        self.assertEqual(data["status"], "limited")
        self.assertEqual(data["sources"], ["https://example.com"])
        self.assertNotIn("5/4", data["summary"])
        self.assertNotIn("limit", data["summary"].lower())

    async def test_recursion_limit_returns_bounded_incomplete_result(self):
        class LoopingWorker(FakeWorker):
            async def ainvoke(self, payload, config=None):
                raise GraphRecursionError("runaway tool loop")

        with patch("src.agent.research_agent.create_agent", return_value=LoopingWorker()):
            tool = build_research_tool(object(), [])
        data = json.loads(await tool.ainvoke({"query": "查询"}))
        self.assertEqual((data["status"], data["sources"]), ("limited", []))

    async def test_sources_come_only_from_tool_results(self):
        result = {"messages": [
            SimpleNamespace(content="", tool_calls=[{"id": "one", "name": "search_web", "args": {"query": "x"}}]),
            SimpleNamespace(content="标题：例子\nURL：https://example.com/page\nURL：javascript:alert(1)", tool_call_id="one", status="success"),
            SimpleNamespace(content="可能还有 https://invented.invalid", tool_calls=[]),
        ]}
        self.assertEqual(json.loads(research_result(result))["sources"], ["https://example.com/page"])

    async def test_worker_receives_query_without_parent_thread_or_history(self):
        worker = FakeWorker()
        with patch("src.agent.research_agent.create_agent", return_value=worker):
            tool = build_research_tool(object(), [])
        result = await tool.ainvoke(
            {"query": "查询资料"},
            config={"configurable": {"thread_id": "private-session"}},
        )
        self.assertEqual(json.loads(result), {"status": "ok", "summary": "资料摘要及来源", "sources": []})
        self.assertEqual(worker.calls[0][0], {"messages": [{"role": "user", "content": "查询资料"}]})
        self.assertNotIn("configurable", worker.calls[0][1])
        self.assertEqual(json.loads(tool.invoke({"query": "另一个问题"}))["summary"], "资料摘要及来源")
        self.assertEqual(worker.calls[1][0]["messages"][0]["content"], "另一个问题")


if __name__ == "__main__":
    unittest.main()
