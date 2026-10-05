import asyncio
import datetime
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Literal, TypeAlias

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import RunnableLambda
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


def _memory_decision_model(schema: type[BaseModel]):
    """Parse decision data from normal text, including Markdown JSON blocks."""
    instructions = (
        "\n\n请根据以下字段定义提供决策数据，可以放在 Markdown 的 ```json 代码块中。"
        "代码块外可以有简短说明。\n字段定义：\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )

    def add_schema(prompt):
        if isinstance(prompt, str):
            return prompt + instructions
        return [*prompt, HumanMessage(content=instructions)]

    return RunnableLambda(add_schema) | llm | PydanticOutputParser(pydantic_object=schema)


candidate_llm = _memory_decision_model(MemoryCandidate)
resolution_llm = _memory_decision_model(MemoryResolutionBatch)
recall_llm = _memory_decision_model(MemoryRecallDecision)

print("candidate_llm==========", candidate_llm)
print("resolution_llm==========", resolution_llm)


def route_memory_keys_with_llm(
    message: str, callbacks: list | None = None
) -> MemoryRecallDecision:
    """让 LLM 区分无需召回和需要召回但没有对应类别。"""
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

5. 如果需要长期记忆，但无法确定上述类别：
should_recall=true
memory_keys=[]

用户问题：

{message}
"""

    config = {"callbacks": callbacks} if callbacks else None
    return recall_llm.invoke(prompt, config=config)


def should_recall_memory(message: str) -> bool:
    """判断“需不需要历史记忆”"""
    # 直接询问过去对话时，即使没有命中具体记忆主题，也交给 LLM 判断。
    explicit_history_phrases = (
        "记得我",
        "我之前说过",
        "我以前说过",
        "之前我说过",
        "以前我说过",
    )
    if any(phrase in message for phrase in explicit_history_phrases):
        return True

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
    """只对明确询问本人事实的完整句式使用免模型精确路由。"""
    question = message.strip().rstrip("？?。.!！").strip()

    if question in {
        "我的职业是什么",
        "我现在的职业是什么",
        "我目前的职业是什么",
        "我现在做什么工作",
        "我目前做什么工作",
        "我现在的工作是什么",
        "我目前的工作是什么",
    }:
        return ["current_job"]

    if question in {
        "我的职业方向是什么",
        "我未来的职业方向是什么",
        "我的发展方向是什么",
        "我未来的发展方向是什么",
        "我的职业规划是什么",
    }:
        return ["career_direction"]

    return []


def should_extract_memory(message: str) -> bool:
    """Skip the extra extraction model call unless text looks like a durable fact."""
    text = message.strip()
    if not text or text.endswith(("?", "？", "吗", "吗？")):
        return False

    personal_assertions = (
        "我是", "我目前", "我现在", "我从事", "我负责", "我正在", "我计划",
        "我打算", "我希望长期", "我的目标", "我的计划", "我的项目", "我喜欢",
        "我不喜欢", "我偏好", "我习惯", "我不吃", "我对",
    )
    durable_topics = (
        "工作", "职业", "岗位", "方向", "目标", "计划", "项目", "学习",
        "技术栈", "偏好", "喜欢", "习惯", "饮食", "过敏", "咖啡", "茶",
        "程序员", "工程师", "开发", "教师", "老师", "设计师", "医生",
        "产品经理", "自由职业", "全职", "兼职",
    )
    return any(marker in text for marker in personal_assertions) and any(
        topic in text for topic in durable_topics
    )


def judge_memory(message: str, callbacks: list | None = None) -> MemoryCandidate:
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

    config = {"callbacks": callbacks} if callbacks else None
    return candidate_llm.invoke(prompt, config=config)


def resolve_memories(
    items: list[dict],
    user_message: str,
    callbacks: list | None = None,
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

    config = {"callbacks": callbacks} if callbacks else None
    return resolution_llm.invoke(prompt, config=config)


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


def _prepare_chat(
    message: str, session_id: str, user_id: str, agent, callbacks: list | None = None
):
    memory_cache: dict[str, list[dict]] = {}

    # =========================
    # 1. Memory Router
    # =========================

    memory_keys: list[str] = []
    recall_source = "none"

    # 门控没有命中时，不做回答前召回；回答后仍可读取旧记忆用于更新判断。
    need_recall = should_recall_memory(message)

    if need_recall:
        memory_keys = route_memory_keys(message)
        if memory_keys:
            recall_source = "rule"
        else:
            decision = route_memory_keys_with_llm(message, callbacks=callbacks)
            if decision.should_recall:
                memory_keys = list(dict.fromkeys(decision.memory_keys))
                recall_source = "llm" if memory_keys else "vector"

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
    if recall_source in ("rule", "llm"):
        recalled_memories = get_memories(memory_keys)

    # LLM 确认需要历史信息但不能确定类别时，才做向量兜底。
    elif recall_source == "vector":
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

            输出格式要求：
            1. 需要分点或分节时，使用标准 Markdown；标题符号后必须有空格，
               标题、段落和列表项各自换行。
            2. 列表项每项单独一行；代码使用带语言标识的围栏代码块。
            3. 中文直接输出，不要将中文转义为 HTML 数字实体（例如 &#x70ED;）。
            """

    agent_input = {"messages": [{"role": "user", "content": message}]}
    agent_config = {
        "configurable": {"thread_id": make_thread_id(user_id, session_id)}
    }
    agent_context = {"system_prompt": system_prompt}

    return memory_cache, get_memories, agent_input, agent_config, agent_context


def _update_long_term_memory(
    message: str, user_id: str, memory_cache, get_memories, callbacks=None
) -> None:

    if not should_extract_memory(message):
        print("跳过长期记忆抽取：当前消息不像长期事实陈述")
        return

    candidate = judge_memory(message, callbacks=callbacks)
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
            resolve_memories(resolve_items, message, callbacks=callbacks), existing_by_key
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

    return None


def chat(message: str, session_id: str, user_id: str, agent) -> str:
    memory_cache, get_memories, agent_input, agent_config, agent_context = _prepare_chat(
        message, session_id, user_id, agent
    )
    result = agent.invoke(agent_input, config=agent_config, context=agent_context)
    answer = result["messages"][-1].content
    _update_long_term_memory(message, user_id, memory_cache, get_memories)
    return answer


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


async def _restore_stopped_turn(agent, config, message_id: str, prompt: str, partial: str) -> None:
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
    message: str, session_id: str, user_id: str, agent, on_token, callbacks=None
) -> tuple[str, str]:
    (
        memory_cache,
        get_memories,
        agent_input,
        agent_config,
        agent_context,
    ) = await asyncio.to_thread(
        _prepare_chat, message, session_id, user_id, agent, callbacks
    )
    message_id = str(uuid.uuid4())
    agent_input["messages"] = [HumanMessage(id=message_id, content=message)]
    partial = ""

    try:
        async for part in agent.astream(
            agent_input,
            config=(
                {**agent_config, "callbacks": callbacks}
                if callbacks
                else agent_config
            ),
            context=agent_context,
            stream_mode=["messages", "updates"],
            version="v2",
        ):
            if part["type"] == "messages":
                chunk, metadata = part["data"]
                if metadata.get("langgraph_node") == "model":
                    token = _content_to_text(chunk.content)
                    if token:
                        partial += token
                        await on_token(token)
            elif part["type"] == "updates":
                model_update = part["data"].get("model", {})
                messages = model_update.get("messages", [])
                if messages:
                    answer = _content_to_text(messages[-1].content)
                    if answer:
                        partial = answer
    except asyncio.CancelledError:
        await asyncio.shield(
            _restore_stopped_turn(agent, agent_config, message_id, message, partial)
        )
        return "stopped", partial
    except Exception:
        await _discard_incomplete_turn(agent, agent_config, message_id)
        raise

    await asyncio.to_thread(
        _update_long_term_memory,
        message,
        user_id,
        memory_cache,
        get_memories,
        callbacks,
    )
    return "done", partial


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
