"""A stateless research worker exposed to the conversation agent as one tool."""

import json
from urllib.parse import urlparse

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.errors import GraphRecursionError


RESEARCH_PROMPT = (
    "你是资料检索 Agent。只处理主 Agent 给出的本次问题。"
    "使用可用的网页和知识库工具查证；返回简短结论、证据和可用的来源链接。"
    "每次任务优先少量有针对性的检索，通常搜索一次、必要时读取一到两页。"
    "如果工具提示调用次数已满，立即依据已取得的资料总结，不再调用工具。"
    "找不到可靠资料时明确说明，不编造来源，不把内部工具限制说成平台或用户限制。"
    "不要处理用户记忆或修改业务数据。"
)


def research_text(result) -> str:
    content = result["messages"][-1].content
    if isinstance(content, str):
        return content or "未找到可用资料。"
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ) or "未找到可用资料。"
    return "未找到可用资料。"


def research_result(result) -> str:
    """Only tool outputs count as sources; worker prose is never source evidence."""
    # 子 Agent 的完整消息只在本次工具执行内使用；返回给主 Agent 的是精简 JSON。
    calls = {}
    sources = []
    limited = False
    for message in result["messages"]:
        for call in getattr(message, "tool_calls", []) or []:
            calls[call.get("id")] = call
        call = calls.get(getattr(message, "tool_call_id", None))
        if not call:
            continue
        content = message.content if isinstance(message.content, str) else ""
        if getattr(message, "status", "success") == "error":
            limited |= "Tool call limit exceeded" in content
            continue
        if call.get("name") == "search_web" and not content.startswith("搜索失败"):
            urls = [line.partition("URL：")[2].strip() for line in content.splitlines() if line.startswith("URL：")]
        elif call.get("name") == "fetch_webpage" and content and not content.startswith("网页访问失败"):
            urls = [call.get("args", {}).get("url", "")]
        else:
            urls = []
        for url in urls:
            parsed = urlparse(url)
            if parsed.scheme in {"http", "https"} and parsed.hostname and url not in sources:
                sources.append(url[:2048])
    summary = (
        "本次检索未完成；若已有足够可靠的资料才回答，否则明确说明无法确认。"
        if limited else research_text(result)
    )
    return json.dumps({
        "status": "limited" if limited else "ok", "summary": summary,
        "sources": sources[:5],
    }, ensure_ascii=False)


def incomplete_research_result() -> str:
    return json.dumps({
        "status": "limited",
        "summary": "本次检索未完成，暂无法核实所需资料；请明确告知用户当前不能确认结论。",
        "sources": [],
    }, ensure_ascii=False)


def build_research_tool(model, search_tools):
    # 子 Agent：有自己的提示词和网页/知识库工具；没有主 Agent 的 checkpointer。
    worker = create_agent(
        model=model,
        tools=search_tools,
        system_prompt=RESEARCH_PROMPT,
        middleware=[ToolCallLimitMiddleware(run_limit=4, exit_behavior="continue")],
    )

    def worker_config(config: RunnableConfig) -> dict:
        # 仅转发追踪/用量回调，不转发主 Agent 的 thread_id 或历史消息。
        return {"callbacks": config.get("callbacks"), "recursion_limit": 16}

    def research_sync(query: str, config: RunnableConfig) -> str:
        # 主 Agent -> 子 Agent：只传本次 query；子 Agent 运行后返回结构化结果。
        try:
            result = worker.invoke(
                {"messages": [{"role": "user", "content": query}]},
                config=worker_config(config),
            )
        except GraphRecursionError:
            return incomplete_research_result()
        return research_result(result)

    async def research_async(query: str, config: RunnableConfig) -> str:
        # SSE 使用异步入口；通信内容与同步入口相同。
        try:
            result = await worker.ainvoke(
                {"messages": [{"role": "user", "content": query}]},
                config=worker_config(config),
            )
        except GraphRecursionError:
            return incomplete_research_result()
        return research_result(result)

    # 子 Agent -> 主 Agent：将结果作为 research 工具返回值交回框架，
    # 框架会把它放进主 Agent 的 ToolMessage；无需共享一个可变变量供主 Agent 读取。
    return StructuredTool.from_function(
        func=research_sync,
        coroutine=research_async,
        name="research",
        description=("当问题需要查证外部事实、网页资料或内部知识库时调用。"
                     "输入一个具体检索问题，返回含 status、summary 与 sources 的 JSON。"
                     "status=limited 仅表示本次内部检索未完成，绝不是平台或用户会话受限；"
                     "普通聊天和用户姓名记忆问答无需调用。"),
    )
