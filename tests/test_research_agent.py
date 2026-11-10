"""The research worker receives only a scoped query and no chat checkpoint."""

import unittest
import json
import uuid
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
    def knowledge_result(self, hits, status="ok"):
        return {"messages": [
            SimpleNamespace(content="", tool_calls=[{"id": "knowledge", "name": "search_knowledge_base", "args": {"query": "测试"}}]),
            SimpleNamespace(content=json.dumps({"status": status, "hits": hits}), tool_call_id="knowledge", status="success"),
            SimpleNamespace(content="研究摘要", tool_calls=[]),
        ]}

    def knowledge_hit(self, text):
        return {"document_id": str(uuid.uuid4()), "chunk_id": str(uuid.uuid4()),
                "title": "虚构资料", "page": 2, "text": text}

    async def test_full_evidence_preserves_tail_without_expanding_source_cards(self):
        text = "背景说明。" * 70 + "关键条件：必须取得安全员签字。"
        hit = self.knowledge_hit(text)
        data = json.loads(research_result(self.knowledge_result([hit, hit])))
        self.assertEqual(data["evidence"], [{"document_id": hit["document_id"],
            "chunk_id": hit["chunk_id"], "title": hit["title"], "page": 2, "text": text}])
        self.assertEqual(len(data["sources"]), 1)
        self.assertEqual(data["sources"][0]["excerpt"], text[:300])
        self.assertNotIn("安全员", data["sources"][0]["excerpt"])

    async def test_evidence_is_bounded_and_only_contains_selected_sources(self):
        hits = [self.knowledge_hit("中" * 1300) for _ in range(5)]
        result = self.knowledge_result(hits)
        # Add a second successful tool call; source and evidence budgets span calls.
        extra = self.knowledge_result([self.knowledge_hit("额外资料")])
        extra["messages"][0].tool_calls[0]["id"] = "extra"
        extra["messages"][1].tool_call_id = "extra"
        result["messages"] = result["messages"][:-1] + extra["messages"]
        data = json.loads(research_result(result))
        self.assertEqual(len(data["evidence"]), 5)
        self.assertLessEqual(sum(len(e["text"].encode()) for e in data["evidence"]), 5 * 4096)
        self.assertEqual({e["chunk_id"] for e in data["evidence"]}, {s["chunk_id"] for s in data["sources"]})

    async def test_unavailable_empty_invalid_and_oversized_results_do_not_supply_evidence(self):
        for status, hits in [
            ("unavailable", [self.knowledge_hit("不可用")]),
            ("no_results", [self.knowledge_hit("无命中")]),
            ("ok", [self.knowledge_hit(" ")]),
            ("ok", [dict(self.knowledge_hit("无效"), chunk_id="invalid")]),
            ("ok", [self.knowledge_hit("中" * 1400)]),
        ]:
            with self.subTest(status=status, hits=hits):
                data = json.loads(research_result(self.knowledge_result(hits, status)))
                self.assertFalse(data.get("evidence"))
                self.assertFalse(data["sources"])

    async def test_sync_and_async_tools_both_deliver_full_evidence(self):
        result = self.knowledge_result([self.knowledge_hit("前文" * 180 + "尾部条件")])
        class KnowledgeWorker(FakeWorker):
            def invoke(self, payload, config=None):
                return result
        with patch("src.agent.research_agent.create_agent", return_value=KnowledgeWorker()):
            tool = build_research_tool(object(), [])
        for raw in (tool.invoke({"query": "测试"}), await tool.ainvoke({"query": "测试"})):
            self.assertTrue(json.loads(raw)["evidence"][0]["text"].endswith("尾部条件"))

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
        self.assertEqual(json.loads(result), {
            "status": "no_results",
            "summary": "本次未找到可核对的资料，不能确认文档或网页中的结论。",
            "sources": [],
        })
        self.assertEqual(worker.calls[0][0], {"messages": [{"role": "user", "content": "查询资料"}]})
        self.assertNotIn("configurable", worker.calls[0][1])
        self.assertEqual(json.loads(tool.invoke({"query": "另一个问题"}))["status"], "no_results")
        self.assertEqual(worker.calls[1][0]["messages"][0]["content"], "另一个问题")


if __name__ == "__main__":
    unittest.main()
