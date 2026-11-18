# AI Gateway 开发进度（2026-11-17）

## M1

- 已实现：Run 领域状态、步数与 Deadline 规则；PR #2 已合并到 `main@9a34458b`。
- 已验证：Python 3.12 本地 8 个测试通过；PR #2 的 GitHub Actions `Offline regression tests` run `44026651631` 成功。
- 边界：纯进程内策略，尚未接入实际 Agent 执行。

## M2 当前进度

- 已实现：`agent_runs` ORM 与前向 SQL 迁移、Run 创建参数、数据库 CAS 状态转换。`/api/chat` 的消息预留与 Run 创建共用事务；模型执行前启动 Run；正常成功、失败或停止时，消息与 Run 终态共用事务。持久截止时间换算为单调时钟剩余时间，主 Agent 图使用 LangGraph 递归上限限制调度步，并将已完成步数 CAS 写回 Run。异常中断或失联后在同一事务标记 `recovery_required`；迟到的自动终态提交不能覆盖待核对记录。
- 已验证：Run、消息存储、流式持久化、HTTP 聊天、启动与 checkpoint 的离线测试通过。过期 Run 不调用模型；运行中超时提交 `timed_out`；真实 LangGraph 循环在上限后未再次安排工具。一次性 PostgreSQL 18 容器测试覆盖迁移重复执行、双连接终态和步数 CAS 竞争、断开连接后读取及请求唯一键；新增子进程强制退出测试确认模拟副作用仅写一次，新请求被 409 阻止并标记待核对。
- 待验证：真实 checkpoint 与外部工具副作用的恢复和对账、研究子 Agent 的步数纳入整体 Run 配额，以及生产迁移顺序和真实数据。模型不响应取消时，Deadline 只能在模型返回或可取消边界落地。
- 下一段：定义人工核对后的恢复操作与审计证据，验证真实 checkpoint 和外部工具的故障行为。

部署前必须先在目标数据库执行 `migrations/20261117_agent_runs.sql`。API 启动会检查 `agent_runs`，缺表时拒绝启动；回滚旧版 API 并确认无运行中的 Run 后，才可删除该表。现有旧消息没有 Run 行，历史重放沿用旧消息状态。

本地原工作区含用户未提交修改，保持原样。隔离 worktree 位于 `/Users/zhangweiwei/Desktop/code/agent-gateway-m2`。本机 GitHub Git 连接不可用，PR 文件经 GitHub 连接器发布；本地与远端提交 SHA 不同，文件差异须以 PR 核对。
