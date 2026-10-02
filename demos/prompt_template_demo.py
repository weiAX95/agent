"""
Agent 项目：Prompt Template 使用 demo

演示三种常见用法：
1. Python 内置 f-string / str.format() —— 最轻量，无需额外依赖。
2. Jinja2 —— 适合复杂模板、条件判断、循环。
3. LangChain PromptTemplate / ChatPromptTemplate —— 与 Agent 框架集成最方便。

依赖：
    pip install openai python-dotenv
    # 如需 Jinja2
    pip install Jinja2
    # 如需 LangChain
    pip install langchain langchain-openai

运行：
    python prompt_template_demo.py
"""

import os
from string import Template

import dotenv
from openai import OpenAI

# 加载 .env 配置
dotenv.load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("api-key")
BASE_URL = os.getenv("OPENAI_BASE_URL") or os.getenv("base-url") or "https://api.openai.com/v1"
MODEL = os.getenv("OPENAI_MODEL", "qwen-plus")


def create_client():
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


def call_llm(client: OpenAI, prompt: str) -> str:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
    )
    return (response.choices[0].message.content or "").strip()


# ==================== 方式 1：Python 内置模板 ====================
def demo_builtin_template():
    print("\n" + "=" * 40)
    print("方式 1：Python 内置模板")
    print("=" * 40)

    # f-string
    role = "资深 Python 工程师"
    topic = "asyncio"
    prompt_f = f"你是一位{role}，请用一句话解释 {topic} 的核心作用。"
    print(f"[f-string]\n{prompt_f}")

    # str.format()
    template = "你是一位{role}，请用一句话解释 {topic} 的核心作用。"
    prompt_format = template.format(role=role, topic=topic)
    print(f"\n[str.format()]\n{prompt_format}")

    # string.Template（适合从配置文件读取模板）
    template_obj = Template("你是一位$role，请用一句话解释 $topic 的核心作用。")
    prompt_template = template_obj.substitute(role=role, topic=topic)
    print(f"\n[string.Template]\n{prompt_template}")


# ==================== 方式 2：Jinja2 模板 ====================
def demo_jinja2_template():
    print("\n" + "=" * 40)
    print("方式 2：Jinja2 模板")
    print("=" * 40)

    try:
        from jinja2 import Template
    except ImportError:
        print("未安装 Jinja2，跳过。安装命令：pip install Jinja2")
        return

    template_str = """
你是一位 {{ role }}。
{% if lang == "zh" %}请用中文回答。{% else %}Please answer in English.{% endif %}
请解释以下概念：{{ topic }}
{% if examples %}并给出 {{ examples|length }} 个例子：{% for ex in examples %}
- {{ ex }}{% endfor %}{% endif %}
""".strip()

    template = Template(template_str)
    prompt = template.render(
        role="AI Agent 专家",
        lang="zh",
        topic="ReAct 推理模式",
        examples=["Chain-of-Thought", "Tool Use", "Observation Reflection"],
    )
    print(prompt)


# ==================== 方式 3：LangChain PromptTemplate ====================
def demo_langchain_template():
    print("\n" + "=" * 40)
    print("方式 3：LangChain PromptTemplate")
    print("=" * 40)

    try:
        from langchain_core.prompts import ChatPromptTemplate, PromptTemplate
    except ImportError:
        print("未安装 langchain / langchain-core，跳过。安装命令：pip install langchain-core")
        return

    # 单轮 PromptTemplate
    template = PromptTemplate.from_template(
        "你是一位{role}，请解释 {topic}，限制在 100 字以内。"
    )
    prompt = template.format(role="大模型专家", topic="RAG")
    print(f"[PromptTemplate]\n{prompt}")

    # 多轮 ChatPromptTemplate
    chat_template = ChatPromptTemplate.from_messages([
        ("system", "你是一位{role}，回答简洁。"),
        ("human", "什么是 {topic}？"),
        ("ai", "我会用一句话概括。"),
        ("human", "请给出实际应用例子。"),
    ])
    messages = chat_template.format_messages(role="AI 产品经理", topic="Agent 记忆模块")
    print("\n[ChatPromptTemplate]")
    for msg in messages:
        print(f"{msg.type}: {msg.content}")


# ==================== 实际调用示例 ====================
def demo_agent_prompt_with_tools():
    print("\n" + "=" * 40)
    print("实际调用：带工具描述的 Agent Prompt")
    print("=" * 40)

    client = create_client()

    system_template = """你是一个 {agent_name}，拥有以下工具：
{tools}

用户问题：{question}

请按以下格式回答：
Thought: 你的思考过程
Action: 你要调用的工具名称（如果需要）
Action Input: 工具参数
Final Answer: 最终答案
"""

    tools_desc = """
- search(query: str): 联网搜索
- calculator(expression: str): 计算表达式
- weather(city: str): 查询城市天气
""".strip()

    prompt = system_template.format(
        agent_name="智能助手",
        tools=tools_desc,
        question="北京今天天气怎么样？",
    )
    print(f"生成的 Prompt:\n{prompt}\n")

    answer = call_llm(client, prompt)
    print(f"模型回答:\n{answer}")


if __name__ == "__main__":
    demo_builtin_template()
    demo_jinja2_template()
    demo_langchain_template()
    demo_agent_prompt_with_tools()
