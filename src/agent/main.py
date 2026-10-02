import datetime
import os
import sys
from pathlib import Path
from typing import Literal, TypeAlias

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain.chat_models import init_chat_model
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.agent.checkpoint import make_thread_id
from src.agent.tools import (
    _get_memories_by_keys,
    _recall_memories,
    fetch_webpage,
    replace_memory,
    save_to_memory,
    search_knowledge_base,
    search_web,
)

os.environ["LANGSMITH_PROJECT"] = "agent-with-memory"

tools = [
    search_web,
    fetch_webpage,
    search_knowledge_base,
]


llm = init_chat_model(
    model=os.getenv("OPENAI_MODEL"),
    model_provider="openai",
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
)


@dynamic_prompt
def conversation_prompt(request: ModelRequest) -> str:
    """Use this turn's recall context without saving it as chat history."""
    return request.runtime.context["system_prompt"]


def build_agent(checkpointer):
    """Build the conversation agent with an application-owned checkpointer."""
    return create_agent(
        model=llm,
        tools=tools,
        checkpointer=checkpointer,
        middleware=[conversation_prompt],
    )


MemoryKey: TypeAlias = Literal[
    "career_direction",
    "learning_direction",
    "long_term_project",
    "work_preference",
    "food_preference",
    "current_job",
]


class MemoryResolutionItem(BaseModel):
    memory_key: MemoryKey
    action: Literal["none", "insert", "replace"]
    memory_id: str | None = None
    memory: str | None = None


class MemoryResolutionBatch(BaseModel):
    resolutions: list[MemoryResolutionItem]


class MemoryCandidateItem(BaseModel):
    memory_key: MemoryKey
    memory: str


# 记忆候选
class MemoryCandidate(BaseModel):
    memories: list[MemoryCandidateItem]


# 记忆回溯决策
class MemoryRecallDecision(BaseModel):
    should_recall: bool
    memory_keys: list[MemoryKey]


candidate_llm = llm.with_structured_output(MemoryCandidate)

resolution_llm = llm.with_structured_output(MemoryResolutionBatch)

recall_llm = llm.with_structured_output(MemoryRecallDecision)

print("candidate_llm==========", candidate_llm)
print("resolution_llm==========", resolution_llm)


def route_memory_keys_with_llm(message: str) -> list[str]:
    """规则判断不了时，让 LLM 路由"""
    prompt = f"""
你负责判断回答当前用户问题需要读取哪些长期记忆。

只能从以下 memory_key 中选择：

current_job
- 用户当前正在从事的职业或岗位

career_direction
- 用户未来计划发展的职业方向

learning_direction
- 用户长期学习方向

long_term_project
- 用户长期维护的项目

work_preference
- 用户对于工作方式、岗位、环境等长期偏好

food_preference
- 用户饮食方面的长期偏好

规则：

1. 只有当回答问题确实需要用户历史信息时，should_recall=true。

2. 一个问题可以需要多个 memory_key。

3. 不允许创建新的 memory_key。

4. 如果不需要长期记忆：
should_recall=false
memory_keys=[]

用户问题：

{message}
"""

    result = recall_llm.invoke(prompt)

    if not result.should_recall:
        return []

    return result.memory_keys


def should_recall_memory(message: str) -> bool:
    """判断“需不需要历史记忆”"""
    personal_keywords = [
        "我",
        "我的",
        "我之前",
        "我以前",
        "我现在",
        "适合我",
        "根据我的",
        "针对我",
        "记得",
        "之前说过",
        "以前说过",
    ]

    memory_topics = [
        "方向",
        "目标",
        "计划",
        "偏好",
        "喜欢",
        "工作",
        "职业",
        "学习",
        "项目",
        "技术栈",
        "习惯",
        "情况",
        "干什么",
        "做什么",
        "背景",
        "经历",
    ]

    has_personal_signal = any(keyword in message for keyword in personal_keywords)

    has_memory_topic = any(keyword in message for keyword in memory_topics)

    return has_personal_signal and has_memory_topic


def route_memory_keys(message: str) -> list[str]:
    """用规则判断需要哪个记忆槽位"""
    keys = set()

    current_job_words = [
        "职业是什么",
        "做什么工作",
        "现在做什么",
        "目前做什么",
        "干什么工作的",
    ]

    career_direction_words = [
        "职业方向",
        "发展方向",
        "职业规划",
        "未来方向",
        "转型方向",
        "以后做什么",
    ]

    if any(word in message for word in current_job_words):
        keys.add("current_job")

    if any(word in message for word in career_direction_words):
        keys.add("career_direction")

    return list(keys)


def judge_memory(message: str) -> MemoryCandidate:
    """判断当前消息值不值得保存"""

    prompt = f"""
    1. 从用户当前消息中提取值得长期保存的事实。

    2. 每一个独立事实单独生成一个 memory item。

    3. 每个 item 必须属于已有 MemoryKey。

    4. 不允许创造新的 memory_key。

    5. current_job 表示当前职业状态；
    career_direction 表示未来职业方向；
    两者必须区分。

    6. learning_direction 表示当前或长期学习方向，
    不等同于职业方向。

    7. 同一个 memory_key 在一次提取中最多返回一个 item。

    8. 如果一句话里同一个 key 有多条描述，
    合并成一条规范化 memory。

    9. 不根据上下文猜测，只提取用户当前消息明确表达的事实。

    10. 没有值得保存的信息时返回 memories=[]。
    禁止：
    - 推断用户经历
    - 补充因果关系
    - 补充时间线
    - 根据已有知识改写成更完整的故事
    - 根据旧记忆推导当前消息没有明确表达的事实

    允许：
    - 对用户原话做简洁规范化
    - 但不能增加新的事实信息
    用户消息：

    {message}
    """

    return candidate_llm.invoke(prompt)


def resolve_memories(
    items: list[dict],
    user_message: str,
) -> MemoryResolutionBatch:

    prompt = f"""
你负责批量管理用户长期记忆。

用户本轮原始消息：
{user_message}

需要判断的新旧记忆：
{items}

对每一个 memory_key 独立判断。

规则：

1. 新记忆与任一旧记忆表达相同事实，且用户没有纠正旧事实：
action = "none"

2. 新记忆是同一类别下的独立事实，未被旧记忆覆盖：
action = "insert"

3. 用户明确修改、纠正、否定某一条已有事实，或同一事实状态变化：
action = "replace"

4. replace 必须从对应 old_memories 中选择目标 memory_id，
memory 返回根据本轮消息整理的最终正确记忆，不能增加新事实。

5. none 和 insert 时，memory_id = null，memory = null。

6. 每个输入 memory_key 必须返回且只能返回一个结果。

7. 不允许新增、删除或改变 memory_key。

8. 不允许根据其他 memory_key 推断当前 memory_key 的事实。

只返回结构化结果。
"""

    return resolution_llm.invoke(prompt)


def validate_memory_resolutions(
    batch: MemoryResolutionBatch,
    existing_by_key: dict[str, list[dict]],
) -> dict[str, MemoryResolutionItem]:
    """整批校验模型决策，避免错误结果造成部分记忆写入。"""
    resolutions = batch.resolutions
    result_by_key = {item.memory_key: item for item in resolutions}
    if (
        len(result_by_key) != len(resolutions)
        or set(result_by_key) != set(existing_by_key)
    ):
        raise ValueError("记忆决策的 memory_key 必须与待处理项一一对应")

    for key, item in result_by_key.items():
        if item.action == "replace":
            valid_ids = {record["id"] for record in existing_by_key[key]}
            if not item.memory_id or item.memory_id not in valid_ids:
                raise ValueError(f"{key} 的替换目标 ID 不在已有记忆中")
            if not item.memory or not item.memory.strip():
                raise ValueError(f"{key} 的替换记忆不能为空")
        elif item.memory_id is not None:
            raise ValueError(f"{key} 的 {item.action} 决策不应指定目标 ID")

    return result_by_key


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
    memory_cache: dict[str, list[dict]] = {}

    # =========================
    # 1. Memory Router
    # =========================

    memory_keys = route_memory_keys(message)
    recall_source = "none"

    # 判断是否需要召回
    need_recall = should_recall_memory(message)

    # 规则命中
    if memory_keys:
        recall_source = "rule"

    # 规则没命中，再让 LLM Router 判断
    elif need_recall:
        memory_keys = route_memory_keys_with_llm(message)

        if memory_keys:
            recall_source = "llm"

    # =========================
    # 2. 内部统一读取方法
    # =========================
    print("\n========== MEMORY ROUTER ==========")
    print("message:", message)
    print("need_recall:", need_recall)
    print("recall_source:", recall_source)
    print("memory_keys:", memory_keys)

    def get_memories(keys: list[str]) -> list[dict]:
        if not keys:
            return []

        # 找出本次请求里还没有查过的 key
        missing_keys = [key for key in keys if key not in memory_cache]

        if missing_keys:
            print(
                "[Milvus Query]",
                "user_id=",
                user_id,
                "keys=",
                missing_keys,
            )
            for key in missing_keys:
                memory_cache[key] = []

            records = _get_memories_by_keys(
                user_id=user_id,
                memory_keys=missing_keys,
            )

            # 有结果就覆盖
            for item in records:
                memory_cache[item["memory_key"]].append(item)
        else:
            print("[Memory Cache Hit]", keys)
        return [item for key in keys for item in memory_cache[key]]

    # =========================
    # 3. 记忆召回
    # =========================

    recalled_memories = []

    # 已经知道 key：精确查询
    if memory_keys:
        recalled_memories = get_memories(memory_keys)

    # 不知道 key：向量召回兜底
    elif need_recall:
        recall_source = "vector"

        recalled_memories = _recall_memories(
            user_id=user_id,
            query=message,
            limit=5,
            min_score=0.4,
        )

        # Top-K 向量结果不是该类别的全部记忆，不能充当完整查询缓存。

    # =========================
    # 4. Memory Context
    # =========================

    if recalled_memories:
        memory_context = "\n".join(
            f"- [{item['memory_key']}] {item['memory']}" for item in recalled_memories
        )
    else:
        memory_context = "暂无相关长期记忆。"

    print("\n========== MEMORY RECALL ==========")
    print("召回来源:", recall_source)
    print("memory_keys:", memory_keys)
    print("召回结果:", recalled_memories)

    system_prompt = f"""
            你是一个带长期记忆能力的 Agent。

            当前用户 ID：
            {user_id}

            当前时间：
            {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

            与当前问题相关的长期记忆：
            {memory_context}

            长期记忆使用规则：

            1. 只在与当前问题相关时使用长期记忆。
            2. 长期记忆只是辅助上下文，不要强行套用。
            3. 如果当前用户明确表达的信息和长期记忆冲突，以当前消息为准。
            4. 不允许根据长期记忆推测用户没有明确表达的信息。
            """

    result = agent.invoke(
        {"messages": [{"role": "user", "content": message}]},
        config={"configurable": {"thread_id": make_thread_id(user_id, session_id)}},
        context={"system_prompt": system_prompt},
    )

    candidate = judge_memory(message)
    candidate_by_key: dict[str, str] = {}
    for item in candidate.memories:
        if item.memory_key in candidate_by_key:
            raise ValueError(f"重复的候选记忆类别: {item.memory_key}")
        if not item.memory.strip():
            raise ValueError(f"{item.memory_key} 的候选记忆不能为空")
        candidate_by_key[item.memory_key] = item.memory.strip()

    existing_by_key: dict[str, list[dict]] = {}
    for item in get_memories(list(candidate_by_key)):
        existing_by_key.setdefault(item["memory_key"], []).append(item)

    resolve_items = [
        {
            "memory_key": key,
            "new_memory": memory,
            "old_memories": [
                {"id": item["id"], "memory": item["memory"]}
                for item in existing_by_key[key]
            ],
        }
        for key, memory in candidate_by_key.items()
        if key in existing_by_key
    ]
    resolution_by_key = (
        validate_memory_resolutions(
            resolve_memories(resolve_items, message), existing_by_key
        )
        if resolve_items
        else {}
    )

    # 决策全部校验通过后再写库；模型输出顺序不影响对应关系。
    for key, memory in candidate_by_key.items():
        resolution = resolution_by_key.get(key)
        if resolution is None or resolution.action == "insert":
            save_to_memory.invoke(
                {"user_id": user_id, "memory": memory, "memory_key": key}
            )
            memory_cache.pop(key, None)
            print("新增记忆:", key, memory)
        elif resolution.action == "replace":
            replacement = resolution.memory.strip()
            replace_memory(
                user_id=user_id,
                memory=replacement,
                memory_key=key,
                memory_id=resolution.memory_id,
            )
            memory_cache.pop(key, None)
            print("替换记忆:", key, resolution.memory_id, replacement)
        else:
            print("重复记忆，不保存:", key)

    return result["messages"][-1].content
