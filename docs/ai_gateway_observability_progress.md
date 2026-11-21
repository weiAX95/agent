# AI Gateway 单次请求诊断视图进度

## 目标与当前结论

当一次回答失败、变慢或费用不明时，登录用户应能找到这次请求，看到 Run 状态、已完成的执行步骤、每次物理模型调用及其用量回执。当前完成的是只读诊断 API；Web 中尚无网关诊断面板，也没有系统级指标、分布式追踪或告警。这些仍归 M5/M8 后续阶段。

## 已实现

- `GET /api/runs/recent?limit=20`：列出当前用户最近的 Run，提供 `session_id`、`request_id`、状态与恢复标记；已删除会话不出现在列表中。
- `GET /api/sessions/{session_id}/requests/{request_id}/diagnostics`：读取该 Run 的步骤、模型物理调用、耗时、trace ID、用量事件及按币种汇总的已确认费用。重试对应独立的 `call_id`。
- 两个接口均使用现有 Bearer 认证和会话归属校验。响应不包含用户提示词、模型回复、密钥或人工核对证据。
- 缺少真实回执或价格时，费用保持 `null`，并计入 `unknown_cost_call_count`；不把未知显示为 0。`selected_cost_event_id` 标明每次调用在汇总中采用的确认回执。

## 如何查看

1. 在后端 `/docs` 点 **Authorize**，输入登录后取得的 Bearer token。
2. 调用 `GET /api/runs/recent`，找到要排查的 `session_id` 与 `request_id`。
3. 调用 `GET /api/sessions/{session_id}/requests/{request_id}/diagnostics`。先看 `run.status`、`run.error_code` 和 `run.recovery_required`，再按顺序看 `steps`、`calls`、`usage_events` 与 `summary`。
4. 发生单次模型调用问题时，使用响应中的 `trace_id` 和 `call_id` 对照服务日志与账本。`/api/chat` 响应头的 `x-trace-id` 也可在浏览器网络面板查看。

## 已验证与边界

离线 API 测试覆盖：Bearer 缺失、跨用户 404、最近请求归属隔离、一次请求含失败重试及成功调用、失败调用费用未知、费用汇总与响应不含聊天正文。CI 将执行相同测试。当前没有真实供应商流量、目标 PostgreSQL 环境或 Web 面板的端到端验收；物理调用的 HTTP 状态码和单次失败原因尚未独立持久化，现阶段可看最终错误码和调用终态。

后续可观测性应补齐：模型调用的结构化错误码与上游 HTTP 状态、延迟分布和错误率指标、跨 Worker trace、预算与限流拒绝指标、可搜索控制面及告警。指标标签只使用低基数枚举，不放用户 ID、请求 ID 或原始内容。
