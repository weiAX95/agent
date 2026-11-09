"""Production chat entrypoints and checkpoint lifecycle."""
import os
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, ToolCallLimitMiddleware, dynamic_prompt
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from src.agent.chat_workflow import ChatServices, build_chat_workflow
from src.agent.checkpoint import make_thread_id
from src.agent.knowledge_store import ensure_knowledge_collection
from src.agent.llm import get_llm
from src.agent.memory.client import close_memory_resources, initialize_memory_resources
from src.agent.memory.context import _prepare_chat
from src.agent.memory.decisions import (
    close_memory_decisions, initialize_memory_decisions, judge_memory, resolve_memories,
    should_extract_memory, validate_memory_resolutions,
)
from src.agent.memory.store import replace_memory, save_to_memory
from src.agent.progress_tool import progress_tool
from src.agent.research_agent import build_research_tool
from src.agent.web_tools import fetch_webpage, search_knowledge_base, search_web

os.environ["LANGSMITH_PROJECT"] = "agent-with-memory"
tools = [search_web, fetch_webpage, search_knowledge_base]

@dynamic_prompt
def conversation_prompt(request: ModelRequest) -> str:
    """Use this turn's recall context without saving it as chat history."""
    return request.runtime.context["system_prompt"]


def build_agent(checkpointer, profile: str | None = None, initialize: bool = True):
    """Build the conversation agent with an application-owned checkpointer."""
    if initialize:
        initialize_memory_resources()
        ensure_knowledge_collection()
        initialize_memory_decisions()
    llm = get_llm(profile)
    # 子 Agent 的入口：这里得到的是名为 research 的工具包装，不是子 Agent 的内部状态。
    # 网页/知识库工具只交给子 Agent；主 Agent 看见的工具是 research 和 report_progress。
    research = build_research_tool(llm, tools)
    # 主 Agent：接收用户问题，决定是否调用 research，并用 checkpointer 保存会话上下文。
    # 调用 research 时，模型给出 query；工具返回 JSON，作为 ToolMessage 进入主 Agent 的消息流。
    return create_agent(
        model=llm,
        tools=[research, progress_tool],
        checkpointer=checkpointer,
        middleware=[
            conversation_prompt,
            ToolCallLimitMiddleware(
                tool_name="research", run_limit=2, exit_behavior="continue"
            ),
            ToolCallLimitMiddleware(
                tool_name="report_progress", run_limit=6, exit_behavior="continue"
            ),
        ],
    )


def close_agent_resources():
    close_memory_decisions()
    close_memory_resources()
    get_llm.cache_clear()


# 记忆debugger
def debug_memory(session_id: str, user_id: str, agent):
    config = {"configurable": {"thread_id": make_thread_id(user_id, session_id)}}

    state = agent.get_state(config)

    print("\n========== STATE ==========")
    print(state)

    print("\n========== MESSAGES ==========")

    for message in state.values["messages"]:
        print(f"\n[{message.type}]")

        print("content:")
        print(message.content)

        if hasattr(message, "tool_calls"):
            print("tool_calls:")
            print(message.tool_calls)



def chat(message: str, session_id: str, user_id: str, agent) -> str:
    result = chat_workflow.invoke(
        {
            "message": message,
            "session_id": session_id,
            "user_id": user_id,
            "agent": agent,
        }
    )
    return result["answer"]


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


async def _restore_stopped_turn(
    agent, config, message_id: str, prompt: str, partial: str
) -> None:
    """Replace transient graph writes with one canonical, explicitly stopped turn."""
    try:
        state = await agent.aget_state(config)
        messages = state.values.get("messages", [])
        start = next(
            (index for index, item in enumerate(messages) if item.id == message_id),
            None,
        )
        if start is not None:
            await agent.aupdate_state(
                config,
                {"messages": [RemoveMessage(id=item.id) for item in messages[start:]]},
            )
    except Exception:
        # If no checkpoint was created, appending a canonical stopped turn is enough.
        pass

    stopped_answer = (partial or "") + "\n\n[本轮已停止生成]"
    await agent.aupdate_state(
        config,
        {
            "messages": [
                HumanMessage(id=message_id, content=prompt),
                AIMessage(content=stopped_answer),
            ]
        },
    )


async def chat_stream(
    message: str,
    session_id: str,
    user_id: str,
    agent,
    on_token,
    callbacks=None,
    on_activity=None,
) -> tuple[str, str, str]:
    result = await chat_workflow.ainvoke(
        {
            "message": message,
            "session_id": session_id,
            "user_id": user_id,
            "agent": agent,
            "on_token": on_token,
            "on_activity": on_activity,
            "callbacks": callbacks,
        }
    )
    memory_status = (
        "skipped"
        if result["status"] == "stopped"
        else (
            "failed"
            if result.get("memory_error")
            else result.get("memory_status", "unknown")
        )
    )
    return result["status"], result["answer"], memory_status


async def _discard_incomplete_turn(agent, config, message_id: str) -> None:
    try:
        state = await agent.aget_state(config)
        messages = state.values.get("messages", [])
        start = next(
            (index for index, item in enumerate(messages) if item.id == message_id),
            None,
        )
        if start is not None:
            await agent.aupdate_state(
                config,
                {"messages": [RemoveMessage(id=item.id) for item in messages[start:]]},
            )
    except Exception:
        pass


chat_workflow = build_chat_workflow(
    ChatServices(
        prepare_chat=_prepare_chat,
        should_extract_memory=should_extract_memory,
        judge_memory=judge_memory,
        resolve_memories=resolve_memories,
        validate_memory_resolutions=validate_memory_resolutions,
        save_to_memory=save_to_memory,
        replace_memory=replace_memory,
        content_to_text=_content_to_text,
        restore_stopped_turn=_restore_stopped_turn,
        discard_incomplete_turn=_discard_incomplete_turn,
    )
)
