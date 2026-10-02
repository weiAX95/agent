import time

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from model import model
from schemas import (
    AgentState,
    MemoryDecision,
    MemoryResolution,
)
from smith import agent
from tools import (
    _get_memories_by_keys,
    _insert_memory,
    replace_memory,
)


def agent_node(state: AgentState):
    start = time.time()
    result = agent.invoke(
        {
            "messages": state["messages"],
        }
    )
    print(f"[TIME] agent_node: {time.time() - start:.2f}s")
    return {
        "messages": result["messages"],
    }


checkpointer = InMemorySaver()

judge_model = model.with_structured_output(MemoryDecision)


def memory_judge_node(state: AgentState):
    start = time.time()
    result = judge_model.invoke(
        [
            {
                "role": "system",
                "content": """
你负责判断本轮对话中，是否出现值得长期保存的用户信息。

应该保存：
1. 用户明确要求记住的信息
2. 长期职业方向
3. 长期目标
4. 稳定的个人偏好
5. 长期项目背景
6. 未来对话中很可能继续有用的信息

不要保存：
1. 普通闲聊
2. 一次性问题
3. 临时信息
4. 普通知识问答
5. 当前天气等短期信息

如果值得保存：
- should_save = true
- 提取简洁、长期有效的 memory
- 给出合适的 memory_key

如果不值得保存：
- should_save = false
- memory_key = null
- memory = null

不要猜测用户没有明确表达的信息。
""",
            },
            *state["messages"],
        ]
    )
    print(f"[TIME] memory_judge: {time.time() - start:.2f}s")

    return {
        "memory_candidate": result.model_dump(),
    }


# =========================
# 3. 判断下一步
# =========================


def should_search_memory(state: AgentState):

    candidate = state.get("memory_candidate")

    if not candidate:
        return "end"

    if candidate.get("should_save") is True:
        return "search"

    return "end"


# =========================
# 4. 搜索已有记忆
# =========================


def search_existing_memory_node(state: AgentState):

    candidate = state["memory_candidate"]

    user_id = state["user_id"]
    memory_key = candidate["memory_key"]

    records = _get_memories_by_keys(
        user_id=user_id,
        memory_keys=[memory_key],
    )

    return {"related_memories": records}


# =========================
# 5. 判断 insert / replace / none
# =========================

resolver_model = model.with_structured_output(MemoryResolution)


def memory_resolver_node(state: AgentState):

    candidate = state["memory_candidate"]

    old_memories = state.get("related_memories", [])

    prompt = f"""
你负责处理用户长期记忆。

新的记忆：

{candidate}

已有相关记忆：

{old_memories}

请判断应该执行什么操作。

action 只能是：

none
insert
replace

规则：

1. none

如果新的记忆和已有记忆本质相同，不需要保存。

例如：

已有：
用户正在往全栈开发方向发展。

新的：
用户决定以后往全栈开发方向发展。

这属于同一条信息。

应该：

action = none


2. insert

如果没有相关旧记忆，
或者新的信息是一个独立的新信息。

应该：

action = insert


3. replace

如果已有记忆与新的记忆属于同一个主题，
但新的信息已经改变或者更新了旧信息。

例如：

旧：
用户主要想做前端开发。

新：
用户现在决定转向全栈开发。

应该：

action = replace

replace 时必须提供需要替换的旧记忆 memory_id。

不要因为文字不同就判断为不同记忆。

要判断它们表达的事实是否相同。

reason 请简洁说明判断原因。
"""

    result = resolver_model.invoke(
        [
            {
                "role": "system",
                "content": prompt,
            }
        ]
    )

    return {"memory_resolution": result.model_dump()}


# =========================
# 6. 真正写入 Milvus
# =========================


def persist_memory_node(state: AgentState):

    candidate = state["memory_candidate"]
    resolution = state["memory_resolution"]

    action = resolution["action"]

    user_id = state["user_id"]

    memory = candidate["memory"]
    memory_key = candidate["memory_key"]

    # 不需要处理
    if action == "none":
        print("[Memory] none")
        return {}

    # 新增
    if action == "insert":

        memory_id = _insert_memory(
            user_id=user_id,
            memory=memory,
            memory_key=memory_key,
        )

        print(f"[Memory] insert: {memory_id}")

        return {}

    # 更新
    if action == "replace":

        memory_id = resolution.get("memory_id")

        if not memory_id:
            raise ValueError("replace 操作必须提供 memory_id")

        related_memories = state.get("related_memories") or []
        related_memory_ids = {item["id"] for item in related_memories}

        if memory_id not in related_memory_ids:
            raise ValueError("replace 操作的 memory_id 不在检索结果中")

        replace_memory(
            user_id=user_id,
            memory=memory,
            memory_key=memory_key,
            memory_id=memory_id,
        )

        print(f"[Memory] replace: {memory_id}")

        return {}

    raise ValueError(f"未知 memory action: {action}")


# =========================
# 7. 构建 Graph
# =========================

builder = StateGraph(AgentState)

builder.add_node(
    "agent",
    agent_node,
)

builder.add_node(
    "memory_judge",
    memory_judge_node,
)

builder.add_node(
    "search_existing_memory",
    search_existing_memory_node,
)

builder.add_node(
    "memory_resolver",
    memory_resolver_node,
)

builder.add_node(
    "persist_memory",
    persist_memory_node,
)


# START
builder.add_edge(
    START,
    "agent",
)


# Agent → Judge
builder.add_edge(
    "agent",
    "memory_judge",
)


# Judge → 条件
builder.add_conditional_edges(
    "memory_judge",
    should_search_memory,
    {
        "search": "search_existing_memory",
        "end": END,
    },
)


# 搜索旧记忆
builder.add_edge(
    "search_existing_memory",
    "memory_resolver",
)


# Resolver
builder.add_edge(
    "memory_resolver",
    "persist_memory",
)


# 持久化
builder.add_edge(
    "persist_memory",
    END,
)

graph = builder.compile(checkpointer=checkpointer)
