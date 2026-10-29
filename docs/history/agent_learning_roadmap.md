# Agent 学习路线

**2026-09-12 历史学习稿。** 当前阶段与下一步统一在 [当前学习路线](../learning_current.md) 维护；本文的完成标记只代表当时的记录。

> 基于项目实际代码定制 · 大模型幻觉研究 · Agent 核心模块：记忆 / 规划 / 行动 / 工具

> 本路线最初写于 2026-09-12，部分代码示例是学习练习，不代表当前正式入口。2026-10-19 的主 API 已使用 [请求级业务图](../graph.md)、PostgreSQL checkpoint 和 POST SSE；当前代码另有 JWT/资源归属实现。当前实现与剩余验收以 [TODO](../TODO.md) 和 [学习进度路线](../learning_progress_roadmap.md) 为准。

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

## Phase 2 ✓ — Tool-Call Agent（历史练习）

**当前代码证据**：`src/agent/main.py`、`src/agent/tools.py`、`src/agent/smith.py`。正式 `main.py` 使用 `create_agent()`；下表中的手写循环是此前的学习主题，不是当前 API 实现。

学习主题是手写 Agent Loop：

```
LLM 调用 → 检查 tool_calls → 执行工具 → ToolMessage 回填 → 循环
```

| 已掌握 | 对应文件 |
|--------|----------|
| `@tool` 装饰器定义工具 | `tools.py` |
| `bind_tools` 与手写循环 | 历史练习；不作为当前 `main.py` 行为依据 |
| `create_agent` 高层封装 | 当前 `main.py`、`smith.py` |

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

## Phase 4 — 对话记忆系统（主流程已接入，继续验收）

> 主 API 的内层 Agent 已使用 PostgreSQL checkpoint 保存多轮历史，并按用户/会话生成复合线程 ID；仍需完整 API 重启、双用户隔离与长期记忆联调。下面的 `MemorySaver` 示例仅供学习，不是现行持久化方案。
>
> **重要说明**：LangChain 旧版 Memory 模块（`ConversationBufferMemory` 等）在 `langchain-core >= 0.3` 中已被标记为 **legacy**，官方推荐迁移到 **LangGraph Checkpointer** 机制。当前项目安装的是 `langchain-core==1.6.2`，应直接学新方案，跳过旧 API。

### 4.1 短期对话记忆 — LangGraph Checkpointer

**目标**：让 Agent 记住当前会话历史，支持连续追问

```python
# ✗ 旧方案（legacy，不推荐新写）
# from langchain.memory import ConversationBufferMemory

# ✓ 新方案：LangGraph Checkpointer
from langgraph.checkpoint.memory import MemorySaver       # 内存存储（开发调试用）
from langgraph.checkpoint.sqlite import SqliteSaver        # SQLite 示例；本项目正式入口使用 PostgreSQL

# 编译 Graph 时注入 checkpointer
app = graph.compile(checkpointer=MemorySaver())

# 每次调用传入 thread_id，LangGraph 自动保存/恢复状态
config = {"configurable": {"thread_id": "user-session-001"}}
result = app.invoke({"messages": [HumanMessage("你好")]}, config=config)

# 下次用同一个 thread_id 调用，历史消息自动带入
result = app.invoke({"messages": [HumanMessage("我刚才说了什么？")]}, config=config)
```

**练习**：用独立示例比较 `MemorySaver` 与正式 PostgreSQL checkpoint 的重启恢复行为，不要把进程内存中的状态当作持久化验证。

---

### 4.2 长期向量记忆

**目标**：跨会话记住用户偏好和历史结论

> 正式入口已通过业务图的 `recall` 节点在回答前读取 Milvus 记忆，并在回答后判断写入；独立学习 Graph 仍有模型可调用的记忆工具。下面是工具设计练习，不是正式入口的代码摘录。

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

**练习**：比较正式业务图的自动召回与独立示例的工具召回，并检查可信身份、重复写入和召回质量；不要直接照搬示例函数作为多用户服务。

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

## Phase 6 — LangGraph 状态机 Agent（业务图已合入）

> 正式 `/chat` 已由 `chat_workflow.py` 的五阶段业务图编排，回答阶段仍复用 `create_agent()` 创建的内层 Agent。下面的代码是状态机练习，不对应正式图的节点命名或边界。

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

**练习目标**：阅读正式业务图的召回、回答、候选判断、冲突决策和写入边界，再与独立示例比较状态和 checkpoint 的职责。

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

正式项目已使用 FastAPI 提供 `/chat` 的 POST SSE 与 `/health`；下面的简化端点只是教学示意，不是现行 API 契约。Gradio 是否使用应另查具体入口。

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
P1：验证当前 JWT 与资源隔离；用真实 PostgreSQL、Milvus、模型完成正式 Graph 的双用户 SSE 联调。
P2：建立请求级观测、召回与回答评测基线；对错误、延迟、token 和费用进行可重复统计。
P3：在上述基线稳定后，比较 Plan-and-Execute、Self-Reflection 等策略是否改善质量。
```

---

*初稿：2026-09-12 · 当前状态标注更新：2026-10-19*
