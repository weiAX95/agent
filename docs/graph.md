# 正式聊天业务图

`/chat` 由 [api/server.py](../api/server.py) 调用 `chat_stream()`，后者运行 [chat_workflow.py](../src/agent/chat_workflow.py) 的请求级 `StateGraph`：

```text
用户消息 → recall → answer → judge ──无候选/失败──→ 结束
                         │
                         └→ resolve ──失败──→ 结束
                               │
                               └→ persist → 结束
```

- `recall`：沿用 `main.py` 的门控、规则、LLM 路由和 Milvus 检索，组装本轮动态提示；查询缓存仅在本轮有效。
- `answer`：调用带 PostgreSQL checkpoint 的内层 Agent。同步调用使用 `invoke()`；正式 SSE 接口使用 `astream()` 逐个发送 token。流式失败清理本轮不完整 checkpoint；断线取消写入“已停止生成”的规范化状态，并跳过后续记忆阶段。
- `judge`：根据当前消息提取长期事实候选；无需提取或候选为空时结束。
- `resolve`：查询同类旧记忆，批量验证 `none`、`insert`、`replace` 决策及替换目标；验证失败时不写入。
- `persist`：将有效决策写入 Milvus；`none` 不写入。

外层图每次调用使用新的临时状态，不另建 checkpoint；内层 Agent 继续用 `make_thread_id(user_id, session_id)` 保存对话历史。召回或回答失败会使请求失败；回答后的记忆阶段失败会记录异常并保留回答。记忆写入不自动重试。具体修改和验证见 [迁移记录](chat_graph_migration.md)。

独立的 [graph.py](../src/agent/graph.py) 仍是学习示例，不是 `/chat` 的入口，也不共享正式 API 的 PostgreSQL checkpoint。
