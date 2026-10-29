# 正式聊天业务图

当前实现说明，更新于 2026-10-29。历史接入过程见 [迁移记录](history/chat_graph_migration.md)。会话和事件表见 [数据结构](session_current.md)。

`/api/chat` 由 [api/server.py](../api/server.py) 调用 `chat_stream()`，后者运行 [chat_workflow.py](../src/agent/chat_workflow.py) 的请求级 `StateGraph`：

```text
用户消息 → recall → answer → judge ──无候选/失败──→ 结束
                         │
                         └→ resolve ──失败──→ 结束
                               │
                               └→ persist → 结束
```

- `recall`：沿用 `main.py` 的门控、规则、LLM 路由和 Milvus 检索，组装本轮动态提示；查询缓存仅在本轮有效。
- `answer`：调用带 PostgreSQL checkpoint 的主 Agent。主 Agent 在需要外部证据时调用 [检索 Agent](../src/agent/research_agent.py)，后者只收到一个查询，使用网页与知识库工具，不持有会话 checkpoint。同步调用使用 `invoke()`；正式 SSE 接口使用 `astream()` 发送主 Agent 的 token。检索、回答、记忆阶段写入 `chat_run_events` 后以 `agent_step` 通知前端；历史消息批量带回这些事件。流式失败清理本轮不完整 checkpoint；断线取消写入“已停止生成”的规范化状态，并跳过后续记忆阶段。
- `judge`：根据当前消息提取长期事实候选；无需提取或候选为空时结束。
- `resolve`：查询同类旧记忆，批量验证 `none`、`insert`、`replace` 决策及替换目标；验证失败时不写入。
- `persist`：将有效决策写入 Milvus；`none` 不写入。

执行过程由 `api/chat_stream.py` 将 `research`、`answer`、`memory` 阶段写入 `chat_run_events`，数据库提交后再发 `agent_step`。主 Agent 的 `report_progress` 也记录为用户可见的 `answer/started` 摘要；模型可能不调用它，不能把摘要称为内部逐字推理。历史读取在归属校验后批量附上事件。前端展示说明见[当前接入](../../web/my-app/docs/chat_current.md)。

外层图每次调用使用新的临时状态，不另建 checkpoint；主 Agent 继续用 `make_thread_id(user_id, session_id)` 保存对话历史。检索 Agent 每次独立执行，结果以工具摘要进入主 Agent 的会话。召回或回答失败会使请求失败；回答后的记忆阶段失败会记录异常并保留回答。记忆写入不自动重试。具体修改和当时的验证见 [迁移记录](history/chat_graph_migration.md)。

## 主 Agent 与检索子 Agent 如何通信

```text
用户消息 → 主 Agent（会话 checkpoint）
              │ 需要查证时调用 research(query)
              ↓
          research 工具包装 → 检索子 Agent（本次独立运行）→ 网页/知识库工具
              │ 返回 {status, summary, sources} JSON
              ↓
          主 Agent 收到工具结果消息 → 继续生成回答
```

- [main.py](../src/agent/main.py) 中的 `create_agent(..., checkpointer=...)` 是主 Agent；`research` 变量是工具包装。主 Agent 只能通过该工具提交查询、读取返回值，不直接读取子 Agent 的局部变量或完整执行状态。
- [research_agent.py](../src/agent/research_agent.py) 中的 `worker = create_agent(...)` 是检索子 Agent。它接收一个 `query`，单独运行检索，把可用结果压缩为 `status`、`summary`、`sources`，再由 `StructuredTool` 返回。它与主 Agent 使用同一个模型配置，但不共享会话历史或 checkpoint。
- 工具框架把返回值放入主 Agent 的工具结果消息，主 Agent 据此继续回答。`query` 和工具返回值是本次通信协议；Python 变量只用于包装和暂存本次结果，不充当两个 Agent 共享的记忆。
- [chat_workflow.py](../src/agent/chat_workflow.py) 是外围业务流程，不是第三个 Agent。它观察主 Agent 的工具调用和结果，生成给前端看的检索进度；`report_progress` 也是主 Agent 的展示工具，不是子 Agent。

独立的 [graph.py](../src/agent/graph.py) 仍是学习示例，不是 `/chat` 的入口，也不共享正式 API 的 PostgreSQL checkpoint。
