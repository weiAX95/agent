# Agent 学习路线

> 基于项目实际代码定制 · 大模型幻觉研究 · Agent 核心模块：记忆 / 规划 / 行动 / 工具

---

## 地图总览

```
[已完成 ✓]                    [当前 →]                  [待解锁]
基础调用 → LangChain LCEL → Tool-Call Agent → RAG基础
                                               ↓
                                        对话记忆系统
                                               ↓
                                      LangGraph 状态机
                                               ↓
                                      多Agent & 规划策略
                                               ↓
                                      可观测性 & 生产部署
```

---

## Phase 0 ✓ — LLM 调用基础

**代码证据**：`demos/batch_demo.py`、`tests/test_api.py`、`tests/test_gemini.py`

| 已掌握 | 对应文件 |
|--------|----------|
| OpenAI 同步/异步调用 | `batch_demo.py` |
| asyncio 并发批量请求 | `batch_demo.py` |
| OpenAI Batch API（JSONL） | `batch_demo.py` |
| 多模型接入（阿里百炼 / Gemini） | `test_api.py`, `test_gemini.py` |
| 环境变量管理 | `.env` + `dotenv` |

---

## Phase 1 ✓ — LangChain 核心组件

**代码证据**：`demos/prompt_template_demo.py`、`demos/runnable_branch_demo.py`

| 已掌握 | 对应文件 |
|--------|----------|
| f-string / Jinja2 / PromptTemplate | `prompt_template_demo.py` |
| ChatPromptTemplate 多轮消息 | `prompt_template_demo.py` |
| LCEL `\|` 管道操作符 | `runnable_branch_demo.py` |
| RunnableBranch 条件路由 | `runnable_branch_demo.py` |
| RunnableParallel 并行执行 | `runnable_branch_demo.py` |
| StrOutputParser | `runnable_branch_demo.py` |

---

## Phase 2 ✓ — Tool-Call Agent（手写循环）

**代码证据**：`src/agent/main.py`、`src/agent/tools.py`、`src/agent/smith.py`

这是项目的**核心成就**，手写了一遍完整的 Agent Loop：

```
LLM 调用 → 检查 tool_calls → 执行工具 → ToolMessage 回填 → 循环
```

| 已掌握 | 对应文件 |
|--------|----------|
| `@tool` 装饰器定义工具 | `tools.py` |
| `bind_tools` 绑定工具到 LLM | `main.py` |
| Agent Loop（最多 N 轮） | `main.py` |
| 工具异常捕获与降级 | `main.py` |
| ToolMessage 回填结果 | `main.py` |
| `create_agent` 高层封装 | `smith.py` |

---

## Phase 3 ✓ — RAG 基础链路

**代码证据**：`scripts/milvus/`、`src/agent/tools.py` 中的 `search_knowledge_base`

| 已掌握 | 对应文件 |
|--------|----------|
| 文档加载（Word / PDF） | `insert_db.py`, `pdf_parser.py` |
| RecursiveCharacterTextSplitter 切分 | `insert_db.py` |
| HuggingFace 本地 Embedding（bge-base-zh） | `insert_db.py`, `tools.py` |
| Milvus Lite 建库 / 写入 / 查询 / 搜索 | `scripts/milvus/*.py` |
| RAG 检索工具（`search_knowledge_base`） | `tools.py` |

---

## Phase 4 → 当前阶段：对话记忆系统

> **现在最该补的短板**。`main.py` 每次运行都是全新对话，没有跨轮次的记忆持久化。
>
> **重要说明**：LangChain 旧版 Memory 模块（`ConversationBufferMemory` 等）在 `langchain-core >= 0.3` 中已被标记为 **legacy**，官方推荐迁移到 **LangGraph Checkpointer** 机制。当前项目安装的是 `langchain-core==1.6.2`，应直接学新方案，跳过旧 API。

### 4.1 短期对话记忆 — LangGraph Checkpointer

**目标**：让 Agent 记住当前会话历史，支持连续追问

```python
# ✗ 旧方案（legacy，不推荐新写）
# from langchain.memory import ConversationBufferMemory

# ✓ 新方案：LangGraph Checkpointer
from langgraph.checkpoint.memory import MemorySaver       # 内存存储（开发调试用）
from langgraph.checkpoint.sqlite import SqliteSaver        # SQLite 持久化（生产用）

# 编译 Graph 时注入 checkpointer
app = graph.compile(checkpointer=MemorySaver())

# 每次调用传入 thread_id，LangGraph 自动保存/恢复状态
config = {"configurable": {"thread_id": "user-session-001"}}
result = app.invoke({"messages": [HumanMessage("你好")]}, config=config)

# 下次用同一个 thread_id 调用，历史消息自动带入
result = app.invoke({"messages": [HumanMessage("我刚才说了什么？")]}, config=config)
```

**练习**：用 LangGraph 改造 `main.py`，加入 `MemorySaver` checkpointer，让同一 `thread_id` 下的对话历史自动持久化。

---

### 4.2 长期向量记忆

**目标**：跨会话记住用户偏好和历史结论

> 旧方案 `VectorStoreRetrieverMemory` 同样是 legacy。新方案是把 Milvus 检索封装为 LangGraph 的工具节点，由 Agent 主动决定何时读写记忆。

```python
# ✓ 新方案：把记忆读写封装为工具，让 Agent 自主调用
@tool
def save_to_memory(content: str) -> str:
    """将重要信息持久化到长期记忆库"""
    vector = embed_model.embed_query(content)
    client.insert(collection_name="memory_collection", data=[{"vector": vector, "text": content}])
    return "已保存到长期记忆"

@tool
def recall_memory(query: str) -> str:
    """从长期记忆库检索相关信息"""
    vector = embed_model.embed_query(query)
    res = client.search(collection_name="memory_collection", data=[vector], limit=3)
    return "\n".join(hit["entity"]["text"] for hit in res[0])
```

**练习**：在 `tools.py` 中新增 `save_to_memory` 和 `recall_memory` 两个工具，接入现有 Milvus + bge 模型，让 Agent 跨会话记住重要结论。

---

### 4.3 对话摘要压缩

**目标**：长对话不超 token 限制

```python
# ✓ 新方案：在 LangGraph 节点中手动实现摘要压缩
from langchain_core.messages import RemoveMessage, SystemMessage

def summarize_node(state):
    messages = state["messages"]
    if len(messages) > 20:  # 超过阈值触发摘要
        summary = llm.invoke("请用3句话总结以下对话：" + str(messages[:-5]))
        # 删除旧消息，插入摘要作为 SystemMessage
        delete = [RemoveMessage(id=m.id) for m in messages[:-5]]
        return {"messages": delete + [SystemMessage(content="对话摘要：" + summary.content)]}
    return state
```

---

## Phase 5 — 结构化输出 & 流式响应

### 5.1 Structured Output（Pydantic 强类型输出）

```python
from pydantic import BaseModel

class ResearchResult(BaseModel):
    summary: str
    sources: list[str]
    confidence: float

# 替代手动解析，LLM 直接返回 Pydantic 对象
llm_with_structure = llm.with_structured_output(ResearchResult)
```

**练习**：把 `main.py` 的最终输出改为结构化格式，而不是纯文本。

---

### 5.2 Streaming 流式输出

```python
# 逐 token 流式输出，提升用户体验
for chunk in llm.stream("解释什么是 Agent"):
    print(chunk.content, end="", flush=True)
```

---

## Phase 6 — LangGraph 状态机 Agent

> LangChain 官方推荐的下一代 Agent 框架，`smith.py` 用的 `create_agent` 是过渡 API，**LangGraph 才是重点**。

### 核心概念

```
State（状态对象）
  ↓
Node（每个节点是一个处理步骤）
  ↓
Edge（普通边 / 条件边）
  ↓
编译成可执行 Graph
```

### 练习路线

```python
from langgraph.graph import StateGraph, END
from typing import TypedDict

# 1. 定义状态
class AgentState(TypedDict):
    messages: list
    next_action: str

# 2. 定义节点（复用已有的工具）
def search_node(state): ...
def rag_node(state): ...
def answer_node(state): ...

# 3. 条件路由（替代 RunnableBranch）
def route(state):
    return "search" if needs_search else "rag"

# 4. 构建图
graph = StateGraph(AgentState)
graph.add_node("search", search_node)
graph.add_conditional_edges("start", route, {...})
app = graph.compile()
```

**改造目标**：把 `main.py` Agent Loop 重写成 LangGraph 版本，体会状态机的优势（可视化、断点、持久化）。

---

## Phase 7 — Agent 规划策略

对应项目的**幻觉研究主题**，这部分最关键：

| 策略 | 说明 | 与项目的关系 |
|------|------|-------------|
| **ReAct** | Thought → Action → Observation 循环 | `main.py` 已实现雏形 |
| **Plan-and-Execute** | 先制定计划，再逐步执行 | 减少幻觉，适合复杂任务 |
| **Self-Reflection** | Agent 自我批判答案质量 | 直接对应幻觉检测 |
| **Multi-Agent 辩论** | 多个 Agent 互相验证 | 高级幻觉抑制方案 |

**练习**：在 `src/agent/` 下新建 `plan_execute.py`，实现一个先规划步骤、再逐一执行的 Agent，对比 `main.py` 的 ReAct 模式哪个幻觉更少。

---

## Phase 8 — 可观测性 & 评估

### LangSmith 追踪

```python
# 已有 langsmith==0.12.1 依赖，但还没用起来
import os
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_API_KEY"] = "your_key"
# 之后所有 LangChain 调用自动记录到 LangSmith 面板
```

**用途**：可视化每一轮 Agent 的 Tool Call、耗时、token 消耗，是调试幻觉问题的利器。

---

## Phase 9 — 生产部署

已有 `fastapi`、`gradio`、`uvicorn` 依赖，但还没用上：

### 9.1 FastAPI 封装 Agent

```python
from fastapi import FastAPI
from langchain_core.messages import HumanMessage

app = FastAPI()

@app.post("/chat")
async def chat(query: str):
    result = agent.invoke({"messages": [HumanMessage(query)]})
    return {"answer": result}
```

### 9.2 Gradio 前端

```python
import gradio as gr

def chat_fn(message, history):
    result = agent.invoke({"messages": [HumanMessage(message)]})
    return result

gr.ChatInterface(chat_fn).launch()
```

---

## 下一步优先级

```
P0（本周）：
  └── Phase 4.1  用 LangGraph MemorySaver checkpointer 改造 main.py（跳过旧版 Memory API）

P1（本月）：
  ├── Phase 5.1  with_structured_output 结构化输出
  └── Phase 6    LangGraph 改造 Agent Loop

P2（进阶）：
  ├── Phase 7    Plan-and-Execute + Self-Reflection（直接服务幻觉研究主题）
  └── Phase 8    LangSmith 接入，可视化追踪每轮推理
```

---

*生成时间：2026-09-12 · 基于项目实际代码分析*
