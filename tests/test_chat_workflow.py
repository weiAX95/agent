"""Offline behavior checks for the request-scoped chat business graph."""

import asyncio
import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from src.agent.chat_workflow import ChatServices, build_chat_workflow


class FakeAgent:
    def __init__(self):
        self.calls = []

    def invoke(self, payload, **kwargs):
        self.calls.append((payload, kwargs))
        return {"messages": [AIMessage(content="回答")]}

    async def astream(self, payload, **kwargs):
        self.calls.append((payload, kwargs))
        yield {
            "type": "messages",
            "data": (AIMessageChunk(content="回"), {"langgraph_node": "model"}),
        }
        yield {
            "type": "messages",
            "data": (AIMessageChunk(content="答"), {"langgraph_node": "model"}),
        }
        yield {
            "type": "updates",
            "data": {"model": {"messages": [AIMessage(content="回答")]}},
        }


def make_services(events, *, write_fails=False):
    def prepare(message, session_id, user_id, agent, callbacks=None):
        events.append("recall")
        return (
            {},
            lambda keys: [],
            {"messages": [{"role": "user", "content": message}]},
            {"configurable": {"thread_id": f"{user_id}:{session_id}"}},
            {"system_prompt": "召回内容"},
        )

    def save(values):
        events.append("persist")
        if write_fails:
            raise RuntimeError("Milvus down")

    async def no_op(*args):
        return None

    return ChatServices(
        prepare_chat=prepare,
        should_extract_memory=lambda message: True,
        judge_memory=lambda message, callbacks=None: SimpleNamespace(
            memories=[SimpleNamespace(memory_key="food_preference", memory="喜欢茶")]
        ),
        resolve_memories=lambda items, message, callbacks=None: None,
        validate_memory_resolutions=lambda batch, existing: {},
        save_to_memory=SimpleNamespace(invoke=save),
        replace_memory=lambda **kwargs: None,
        content_to_text=lambda content: content if isinstance(content, str) else "",
        restore_stopped_turn=no_op,
        discard_incomplete_turn=no_op,
    )


class ChatWorkflowTests(unittest.TestCase):
    def test_sync_graph_runs_recall_answer_and_memory_stages(self):
        events = []
        agent = FakeAgent()
        workflow = build_chat_workflow(make_services(events))
        result = workflow.invoke(
            {"message": "我喜欢茶", "session_id": "s1", "user_id": "alice", "agent": agent}
        )
        self.assertEqual(result["answer"], "回答")
        self.assertEqual(result["memory_status"], "saved")
        self.assertEqual(events, ["recall", "persist"])
        self.assertEqual(agent.calls[0][1]["config"]["configurable"]["thread_id"], "alice:s1")

    def test_memory_failure_after_answer_keeps_answer(self):
        events = []
        workflow = build_chat_workflow(make_services(events, write_fails=True))
        with self.assertLogs("src.agent.chat_workflow", level="ERROR"):
            result = workflow.invoke(
                {"message": "我喜欢茶", "session_id": "s1", "user_id": "alice", "agent": FakeAgent()}
            )
        self.assertEqual(result["answer"], "回答")
        self.assertEqual(result["memory_error"], "persist")
        self.assertEqual(result["memory_status"], "pending")


class ChatStreamWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_limited_research_reports_incomplete_instead_of_completed(self):
        class LimitedAgent(FakeAgent):
            async def astream(self, payload, **kwargs):
                yield {"type": "updates", "data": {"model": {"messages": [AIMessage(
                    content="", tool_calls=[{"id": "call-1", "name": "research", "args": {"query": "天气"}}]
                )]}}}
                yield {"type": "updates", "data": {"tools": {"messages": [ToolMessage(
                    tool_call_id="call-1",
                    content='{"status":"limited","summary":"无法确认","sources":[]}',
                )]}}}
                yield {"type": "updates", "data": {"model": {"messages": [AIMessage(content="无法确认实时天气")]}}}

        activities = []
        async def on_activity(agent, status, details=None):
            activities.append((agent, status, details))
        async def on_token(token):
            pass
        await build_chat_workflow(make_services([])).ainvoke({
            "message": "天气", "session_id": "s1", "user_id": "alice",
            "agent": LimitedAgent(), "on_token": on_token, "on_activity": on_activity,
        })
        self.assertEqual(activities[-1], ("research", "failed", {"sources": [], "reason": "limited"}))

    async def test_model_progress_tool_is_published_before_answer_tokens(self):
        class ProgressAgent(FakeAgent):
            async def astream(self, payload, **kwargs):
                yield {"type": "updates", "data": {"model": {"messages": [AIMessage(
                    content="", tool_calls=[{"id": "progress-1", "name": "report_progress",
                                             "args": {"summary": "先核对问题里的时间范围"}}]
                )]}}}
                yield {"type": "messages", "data": (
                    AIMessageChunk(content="答案"), {"langgraph_node": "model"}
                )}
                yield {"type": "updates", "data": {"model": {"messages": [AIMessage(content="答案")]}}}

        observed = []
        async def on_activity(agent, status, details=None):
            observed.append((agent, status, details))
        async def on_token(token):
            observed.append(("token", token))
        result = await build_chat_workflow(make_services([])).ainvoke({
            "message": "复杂问题", "session_id": "s1", "user_id": "alice",
            "agent": ProgressAgent(), "on_token": on_token, "on_activity": on_activity,
        })
        self.assertEqual(observed[:2], [
            ("progress", "started", {"summary": "先核对问题里的时间范围"}),
            ("token", "答案"),
        ])
        self.assertEqual(result["answer"], "答案")

    async def test_research_updates_report_progress_without_streaming_tool_result(self):
        class ResearchAgent(FakeAgent):
            async def astream(self, payload, **kwargs):
                yield {"type": "updates", "data": {"model": {"messages": [AIMessage(
                    content="", tool_calls=[{"id": "call-1", "name": "research", "args": {"query": "资料"}}]
                )]}}}
                yield {"type": "updates", "data": {"tools": {"messages": [ToolMessage(
                    tool_call_id="call-1", content="内部资料，不应直出"
                )]}}}
                yield {"type": "messages", "data": (
                    AIMessageChunk(content="结论"), {"langgraph_node": "model"}
                )}
                yield {"type": "updates", "data": {"model": {"messages": [AIMessage(content="结论")]}}}

        activities = []
        tokens = []
        async def on_activity(agent, status, details=None): activities.append((agent, status))
        async def on_token(token): tokens.append(token)
        workflow = build_chat_workflow(make_services([]))
        result = await workflow.ainvoke({
            "message": "查资料", "session_id": "s1", "user_id": "alice",
            "agent": ResearchAgent(), "on_token": on_token, "on_activity": on_activity,
        })
        self.assertEqual(activities, [("research", "started"), ("research", "completed")])
        self.assertEqual(tokens, ["结论"])
        self.assertEqual(result["answer"], "结论")

    async def test_async_graph_emits_tokens_and_returns_complete_answer(self):
        events = []
        tokens = []
        workflow = build_chat_workflow(make_services(events))

        async def on_token(token):
            tokens.append(token)

        result = await workflow.ainvoke(
            {
                "message": "我喜欢茶",
                "session_id": "s1",
                "user_id": "alice",
                "agent": FakeAgent(),
                "on_token": on_token,
            }
        )
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["answer"], "回答")
        self.assertEqual(tokens, ["回", "答"])
        self.assertEqual(events, ["recall", "persist"])

    async def test_cancelled_stream_restores_stopped_turn_and_skips_memory(self):
        started = asyncio.Event()
        restored = []
        events = []

        class SlowAgent(FakeAgent):
            async def astream(self, payload, **kwargs):
                yield {
                    "type": "messages",
                    "data": (AIMessageChunk(content="部分"), {"langgraph_node": "model"}),
                }
                await asyncio.sleep(3600)

        async def on_token(token):
            started.set()

        async def restore(*args):
            restored.append(args)

        services = make_services(events)
        services = ChatServices(
            **{**services.__dict__, "restore_stopped_turn": restore}
        )
        workflow = build_chat_workflow(services)
        task = asyncio.create_task(workflow.ainvoke({
            "message": "问题", "session_id": "s1", "user_id": "alice",
            "agent": SlowAgent(), "on_token": on_token,
        }))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        result = await asyncio.wait_for(task, timeout=2)
        self.assertEqual((result["status"], result["answer"]), ("stopped", "部分"))
        self.assertEqual(len(restored), 1)
        self.assertEqual(events, ["recall"])


if __name__ == "__main__":
    unittest.main()
