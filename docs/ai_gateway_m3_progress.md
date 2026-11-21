# AI Gateway M3 开发进度（2026-11-21）

M2 的 PR #3 与 M3 前两段的 PR #4 已合并到 `main`。本段继续实现每次物理模型调用的独立计量与费用账本。

- 已设计：价格版本与供应商 token 用量分开保存；缺失上报保持 `NULL`，缓存输入从总输入中扣除后按专属费率计算；使用 `Decimal`，不使用浮点货币。
- 已实现：纯领域 `TokenUsage`、`PriceVersion` 和 `calculate_cost`；价格版本、物理调用与用量回执 ORM 和正式 SQL 迁移。调用起点固定价格版本；调用登记、CAS 终态、只插入回执及同键幂等冲突检查已实现。数据库触发器禁止历史价格、回执及人工核对审计更新/删除。Run 汇总按调用只选最新已确认回执，按币种分别计费。OpenAI SDK 使用计量 HTTP 客户端，每次实际发送在请求前独立登记；JSON 或 SSE 供应商用量追加到原 call_id。失联核对只处理超过 Run 截止时间与宽限期的调用，标为未知并阻断自动续跑；迟到传输证据和费用仍可追加。运维命令默认只读，执行要求核对人和证据引用，并与裁决同事务写入审计。缺少 M3 迁移时 API 拒绝启动。
- 已验证：Python 3.12 本地领域、表约束、存储、传输、运维参数与 `/api/chat` 回归通过；一次性 PostgreSQL 18 容器专项覆盖重复迁移、Decimal 金额、迟到回执、同步和异步模拟 HTTP 请求写入、运维预览/执行、审计不可变。SDK 实际 429 重试测试确认两次发送产生两个不同 call_id；JSON 与 SSE 分块解析在本地通过。PR #5 上次远端 CI run `44877272884` 成功；本次审计提交的 CI 待推送后核对。
- 下一段：验证真实模型流式 usage 格式与供应商对账，演练目标环境迁移和回滚。无用量上报时费用保持 NULL。

### 迁移与失联核对

目标数据库先执行 `20261118_gateway_usage.sql`，再依次执行 `20261120_gateway_call_price_snapshot.sql`、`20261120_gateway_ledger_append_only.sql`、`20261121_gateway_call_reconciliations.sql`。这些迁移依赖已完成的 M2 Run 迁移；在目标环境执行前仍需备份与演练。

`PYTHONPATH=. python -m tools.reconcile_model_calls` 仅预览过期 call_id。核对供应商请求与外部结果后，使用 `PYTHONPATH=. python -m tools.reconcile_model_calls --apply --reviewer-id <UUID> --evidence-ref <证据引用>` 标记未知并记录审计。命令使用 `DATABASE_URL`，不自动重发请求，也不将未知费用改为零。

M2 的正式生产迁移和第三方工具联调仍独立待验收。原工作区的未提交修改保持原样。
