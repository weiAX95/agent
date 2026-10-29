# 当前会话、消息与执行事件

更新日期：2026-10-29。实际结构以 [ORM](../api/models.py) 和 [迁移目录](../migrations/) 为准。首版设计及当时未实施项保留在 [历史设计稿](chat_session_schema.md)。

## 数据归属

`users.id`（UUID）一对多关联 `chat_sessions.owner_id`；会话一对多关联 `chat_messages.session_id`。每轮用同一个 `request_id` 关联一条 user 与一条 assistant 消息，`seq` 保持会话内顺序。`chat_run_events.message_id` 指向 assistant 消息，按 `sequence` 保存检索、回答、记忆阶段的状态、摘要和来源。`chat_logs` 与 `chat_feedback` 保留旧反馈关联；它们不承担完整消息或 checkpoint 状态。

PostgreSQL checkpoint 保存主 Agent 的模型上下文和工具消息，使用用户与会话组成的稳定 `thread_id`；`chat_messages` 保存用户界面的真实消息状态。检索子 Agent没有自己的会话 checkpoint。Milvus 个人记忆按用户存取，与 session 生命周期独立。

## 写入与读取

- 创建会话时由服务端生成 UUID 并绑定 JWT 当前用户；客户端不指定 `owner_id`。列表只返回本人、未软删除且有消息的会话。
- `/api/chat` 先校验归属，再对同一会话取得 PostgreSQL advisory lock。新请求占位写入 user/completed 与 assistant/streaming；同 `request_id` 同正文重放已存结果，不重新调用模型；不同正文返回冲突。
- 生成中节流保存部分回答；终态为 `completed`、`stopped`、`failed` 或 `interrupted`。记忆处理结果另存 `memory_status`。数据库事件提交后才发送 SSE `agent_step`；终态提交后才发 `done`、`stopped` 或 `error`。
- `GET /api/sessions` 用活动时间与 ID 游标分页；`GET /api/sessions/{id}/messages` 先检查归属，按 `seq` 分页并批量附上执行事件。前端首次显示最近 100 条，再按需加载旧消息。其他用户、已删除或不存在的会话统一不返回历史。
- checkpoint、业务消息与 Milvus 使用不同存储事务，不宣称跨三者原子一致；异常中断的请求不会自动盲目重跑。

建表与增量迁移：[会话消息](../migrations/20261023_chat_history.sql)、[记忆状态](../migrations/20261027_chat_memory_status.sql)、[执行事件](../migrations/20261027_chat_run_events.sql)。当前项目是否已经在其他数据库执行这些迁移，需要按该环境实际状态核对。
