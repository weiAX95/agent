"""Request-scoped chat graph; the inner agent owns conversation checkpoints."""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


class ChatState(TypedDict, total=False):
    message: str
    session_id: str
    user_id: str
    agent: Any
    callbacks: list | None
    on_token: Callable
    memory_cache: dict
    get_memories: Callable
    agent_input: dict
    agent_config: dict
    agent_context: dict
    answer: str
    status: str
    candidate_by_key: dict[str, str]
    resolution_by_key: dict
    memory_error: str
    memory_status: str


@dataclass(frozen=True)
class ChatServices:
    prepare_chat: Callable
    should_extract_memory: Callable
    judge_memory: Callable
    resolve_memories: Callable
    validate_memory_resolutions: Callable
    save_to_memory: Any
    replace_memory: Callable
    content_to_text: Callable
    restore_stopped_turn: Callable
    discard_incomplete_turn: Callable


def build_chat_workflow(services: ChatServices):
    """Compile once; each invocation supplies fresh state and no outer checkpoint."""

    def recall(state: ChatState) -> dict:
        cache, get_memories, payload, config, context = services.prepare_chat(
            state["message"],
            state["session_id"],
            state["user_id"],
            state["agent"],
            state.get("callbacks"),
        )
        return {
            "memory_cache": cache,
            "get_memories": get_memories,
            "agent_input": payload,
            "agent_config": config,
            "agent_context": context,
        }

    async def arecall(state: ChatState) -> dict:
        return await asyncio.to_thread(recall, state)

    def answer(state: ChatState) -> dict:
        result = state["agent"].invoke(
            state["agent_input"],
            config=state["agent_config"],
            context=state["agent_context"],
        )
        return {"answer": result["messages"][-1].content, "status": "done"}

    async def aanswer(state: ChatState) -> dict:
        message_id = str(uuid.uuid4())
        payload = dict(state["agent_input"])
        payload["messages"] = [HumanMessage(id=message_id, content=state["message"])]
        config = state["agent_config"]
        partial = ""
        try:
            async for part in state["agent"].astream(
                payload,
                config=(
                    {**config, "callbacks": state["callbacks"]}
                    if state.get("callbacks")
                    else config
                ),
                context=state["agent_context"],
                stream_mode=["messages", "updates"],
                version="v2",
            ):
                if part["type"] == "messages":
                    chunk, metadata = part["data"]
                    if metadata.get("langgraph_node") == "model":
                        token = services.content_to_text(chunk.content)
                        if token:
                            partial += token
                            await state["on_token"](token)
                elif part["type"] == "updates":
                    messages = part["data"].get("model", {}).get("messages", [])
                    if messages:
                        complete = services.content_to_text(messages[-1].content)
                        if complete:
                            partial = complete
        except asyncio.CancelledError:
            await asyncio.shield(
                services.restore_stopped_turn(
                    state["agent"], config, message_id, state["message"], partial
                )
            )
            return {"answer": partial, "status": "stopped"}
        except Exception:
            await services.discard_incomplete_turn(state["agent"], config, message_id)
            raise
        return {"answer": partial, "status": "done"}

    def judge(state: ChatState) -> dict:
        if not services.should_extract_memory(state["message"]):
            print("[Memory Extract] skipped: empty message")
            return {"candidate_by_key": {}, "memory_status": "skipped"}
        candidate = services.judge_memory(
            state["message"], callbacks=state.get("callbacks")
        )
        by_key = {}
        for item in candidate.memories:
            if item.memory_key in by_key:
                raise ValueError(f"重复的候选记忆类别: {item.memory_key}")
            if not item.memory.strip():
                raise ValueError(f"{item.memory_key} 的候选记忆不能为空")
            by_key[item.memory_key] = item.memory.strip()
        print("[Memory Extract] candidate_keys=", list(by_key))
        return {"candidate_by_key": by_key, "memory_status": "pending" if by_key else "skipped"}

    def resolve(state: ChatState) -> dict:
        by_key = state["candidate_by_key"]
        existing = {}
        for item in state["get_memories"](list(by_key)):
            existing.setdefault(item["memory_key"], []).append(item)
        items = [
            {
                "memory_key": key,
                "new_memory": memory,
                "old_memories": [
                    {"id": item["id"], "memory": item["memory"]}
                    for item in existing[key]
                ],
            }
            for key, memory in by_key.items()
            if key in existing
        ]
        decisions = (
            services.validate_memory_resolutions(
                services.resolve_memories(
                    items, state["message"], callbacks=state.get("callbacks")
                ),
                existing,
            )
            if items
            else {}
        )
        return {"resolution_by_key": decisions}

    def persist(state: ChatState) -> dict:
        changed = False
        for key, memory in state["candidate_by_key"].items():
            decision = state["resolution_by_key"].get(key)
            if decision is None or decision.action == "insert":
                services.save_to_memory.invoke(
                    {
                        "user_id": state["user_id"],
                        "memory": memory,
                        "memory_key": key,
                    }
                )
                changed = True
                print("[Memory Persist] insert succeeded: key=", key)
            elif decision.action == "replace":
                services.replace_memory(
                    user_id=state["user_id"],
                    memory=decision.memory.strip(),
                    memory_key=key,
                    memory_id=decision.memory_id,
                )
                changed = True
                print("[Memory Persist] replace succeeded: key=", key)
            else:
                print("[Memory Persist] unchanged: key=", key)
        return {"memory_status": "saved" if changed else "unchanged"}

    def safe(name: str, operation: Callable):
        def run(state: ChatState) -> dict:
            try:
                return operation(state)
            except Exception:
                logger.exception("Chat memory stage failed: %s", name)
                return {"memory_error": name}

        async def arun(state: ChatState) -> dict:
            return await asyncio.to_thread(run, state)

        return RunnableLambda(run, afunc=arun)

    workflow = StateGraph(ChatState)
    workflow.add_node("recall", RunnableLambda(recall, afunc=arecall))
    workflow.add_node("answer", RunnableLambda(answer, afunc=aanswer))
    workflow.add_node("judge", safe("judge", judge))
    workflow.add_node("resolve", safe("resolve", resolve))
    workflow.add_node("persist", safe("persist", persist))
    workflow.add_edge(START, "recall")
    workflow.add_edge("recall", "answer")
    workflow.add_conditional_edges(
        "answer", lambda state: END if state.get("status") == "stopped" else "judge"
    )
    workflow.add_conditional_edges(
        "judge",
        lambda state: (
            END
            if state.get("memory_error") or not state.get("candidate_by_key")
            else "resolve"
        ),
    )
    workflow.add_conditional_edges(
        "resolve", lambda state: END if state.get("memory_error") else "persist"
    )
    workflow.add_edge("persist", END)
    graph = workflow.compile()
    print(graph.get_graph().draw_mermaid())

    return graph
