"""
Agent 项目：RunnableBranch 调用方式 demo

RunnableBranch 是 LangChain LCEL 的条件分支组件，
根据输入依次匹配条件，命中则执行对应 Runnable，否则执行 default。

依赖：
    pip install langchain langchain-openai openai python-dotenv

运行：
    python runnable_branch_demo.py
"""

import os

import dotenv
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableBranch, RunnableLambda, RunnableParallel
from langchain_openai import ChatOpenAI

# 加载 .env 配置
dotenv.load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("api-key")
BASE_URL = os.getenv("OPENAI_BASE_URL") or os.getenv("base-url") or "https://api.openai.com/v1"
MODEL = os.getenv("OPENAI_MODEL", "qwen-plus")

# 初始化模型
llm = ChatOpenAI(
    model=MODEL,
    openai_api_key=API_KEY,
    openai_api_base=BASE_URL,
    temperature=0.7,
    max_tokens=200,
)


# ==================== 示例 1：基础 RunnableBranch ====================
def demo_basic_branch():
    print("\n" + "=" * 50)
    print("示例 1：基础 RunnableBranch")
    print("=" * 50)

    # 条件函数：接收输入，返回 bool
    def is_greeting(text: str) -> bool:
        return any(word in text.lower() for word in ["你好", "hello", "hi"])

    def is_math(text: str) -> bool:
        return any(word in text for word in ["计算", "+", "-", "*", "/", "等于"])

    # 各个分支的处理逻辑
    greeting_chain = RunnableLambda(lambda x: "你好！有什么可以帮你的吗？")
    math_chain = RunnableLambda(lambda x: f"收到数学问题：{x}，我会调用 calculator 工具。")
    default_chain = RunnableLambda(lambda x: f"收到一般问题：{x}，我会直接回答。")

    branch = RunnableBranch(
        (is_greeting, greeting_chain),
        (is_math, math_chain),
        default_chain,
    )

    test_inputs = [
        "你好，帮我个忙",
        "计算 23 + 45 等于多少",
        "什么是 agent？",
    ]

    for text in test_inputs:
        result = branch.invoke(text)
        print(f"输入: {text}")
        print(f"输出: {result}\n")


# ==================== 示例 2：分支返回不同 Prompt + LLM ====================
def demo_llm_branch():
    print("\n" + "=" * 50)
    print("示例 2：根据问题类型路由到不同 LLM Prompt")
    print("=" * 50)

    def is_coding_question(data: dict) -> bool:
        return data.get("type") == "coding"

    def is_business_question(data: dict) -> bool:
        return data.get("type") == "business"

    coding_prompt = ChatPromptTemplate.from_messages([
        ("system", "你是一位资深程序员，回答要包含代码示例。"),
        ("human", "{question}"),
    ])

    business_prompt = ChatPromptTemplate.from_messages([
        ("system", "你是一位商业顾问，回答要突出商业价值。"),
        ("human", "{question}"),
    ])

    default_prompt = ChatPromptTemplate.from_messages([
        ("system", "你是一位通用助手，回答简洁。"),
        ("human", "{question}"),
    ])

    coding_chain = coding_prompt | llm | StrOutputParser()
    business_chain = business_prompt | llm | StrOutputParser()
    default_chain = default_prompt | llm | StrOutputParser()

    branch = RunnableBranch(
        (is_coding_question, coding_chain),
        (is_business_question, business_chain),
        default_chain,
    )

    test_cases = [
        {"type": "coding", "question": "如何用 Python 实现一个简单 agent？"},
        {"type": "business", "question": "agent 在企业客服中有什么价值？"},
        {"type": "other", "question": "今天天气怎么样？"},
    ]

    for data in test_cases:
        result = branch.invoke(data)
        print(f"类型: {data['type']}, 问题: {data['question']}")
        print(f"回答: {result[:100]}...\n")


# ==================== 示例 3：Agent 路由（结合工具意图识别）====================
def demo_agent_intent_branch():
    print("\n" + "=" * 50)
    print("示例 3：Agent 意图识别 + RunnableBranch 路由")
    print("=" * 50)

    def needs_search(data: dict) -> bool:
        return "搜索" in data["intent"] or "search" in data["intent"]

    def needs_calculator(data: dict) -> bool:
        return "计算" in data["intent"] or "calculator" in data["intent"]

    def needs_weather(data: dict) -> bool:
        return "天气" in data["intent"] or "weather" in data["intent"]

    # 模拟工具调用链
    search_chain = RunnableLambda(
        lambda x: {"tool": "search", "input": x["query"], "result": f"搜索结果：{x['query']} 的最新信息"}
    )
    calculator_chain = RunnableLambda(
        lambda x: {"tool": "calculator", "input": x["query"], "result": "计算结果：42"}
    )
    weather_chain = RunnableLambda(
        lambda x: {"tool": "weather", "input": x["query"], "result": "北京今天晴，25°C"}
    )
    default_chain = RunnableLambda(
        lambda x: {"tool": "direct_answer", "input": x["query"], "result": "直接回答用户问题"}
    )

    branch = RunnableBranch(
        (needs_search, search_chain),
        (needs_calculator, calculator_chain),
        (needs_weather, weather_chain),
        default_chain,
    )

    # 意图识别链（这里简化为规则，实际可用 LLM 做意图分类）
    def classify_intent(data: dict) -> dict:
        query = data["query"]
        if any(k in query for k in ["搜索", "查一下", "最新"]):
            intent = "search"
        elif any(k in query for k in ["计算", "等于", "+"]):
            intent = "calculator"
        elif any(k in query for k in ["天气", "气温"]):
            intent = "weather"
        else:
            intent = "direct"
        return {"query": query, "intent": intent}

    agent = RunnableLambda(classify_intent) | branch

    test_queries = [
        "搜索一下最近的 AI 新闻",
        "计算 100 除以 2 加 3",
        "北京今天天气怎么样",
        "讲一个笑话",
    ]

    for query in test_queries:
        result = agent.invoke({"query": query})
        print(f"问题: {query}")
        print(f"路由: {result}\n")


# ==================== 示例 4：与 RunnableParallel 组合 ====================
def demo_parallel_then_branch():
    print("\n" + "=" * 50)
    print("示例 4：先并行处理，再分支汇总")
    print("=" * 50)

    def is_long(data: dict) -> bool:
        return len(data["summary"]) > 50

    # 并行生成摘要和关键词
    parallel = RunnableParallel(
        summary=ChatPromptTemplate.from_template("请总结以下内容：\n{content}") | llm | StrOutputParser(),
        keywords=ChatPromptTemplate.from_template("请提取 3 个关键词：\n{content}") | llm | StrOutputParser(),
    )

    # 长摘要走精炼分支，短摘要直接输出
    refine_chain = RunnableLambda(
        lambda x: {"summary": x["summary"][:50] + "...", "keywords": x["keywords"]}
    )
    direct_chain = RunnableLambda(lambda x: x)

    branch = RunnableBranch(
        (is_long, refine_chain),
        direct_chain,
    )

    pipeline = parallel | branch

    content = "Agent 是一种能够感知环境、做出决策并执行动作的智能系统。它通常包含记忆、规划、行动和工具调用等模块。"
    result = pipeline.invoke({"content": content})
    print(f"输入: {content}")
    print(f"输出: {result}\n")


if __name__ == "__main__":
    demo_basic_branch()
    demo_llm_branch()
    demo_agent_intent_branch()
    demo_parallel_then_branch()
