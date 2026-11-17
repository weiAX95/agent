# AI Gateway 开发进度（2026-11-17）

## M1

- 已实现：Run 领域状态、步数与 Deadline 规则；PR #2 已合并到 `main@9a34458b`。
- 已验证：Python 3.12 本地 8 个测试通过；PR #2 的 GitHub Actions `Offline regression tests` run `44026651631` 成功。
- 边界：纯进程内策略，尚未接入实际 Agent 执行。

## M2 当前进度

- 已实现：`agent_runs` ORM 与前向 SQL 迁移、Run 创建参数、数据库 CAS 状态转换。`/api/chat` 的消息预留与 Run 创建共用事务；模型执行前启动 Run；正常成功、失败或停止时，消息与 Run 终态共用事务。
- 已验证：离线 SQLite Run 测试 10 个、消息存储测试 11 个、流式持久化测试 11 个、HTTP 聊天测试 5 个、启动与 checkpoint 测试 15 个通过。模拟 Run 终态竞争时，消息更新会回滚。一次性 PostgreSQL 18 容器中，迁移重复执行、双连接终态 CAS 竞争、断开连接后读取和重复请求唯一键共 2 个集成测试通过。
- 待验证：真实进程强杀后的 checkpoint/工具副作用恢复，以及 M1 的 Deadline 和调度步数在 Agent 图中的实际执行限制。PostgreSQL 测试未覆盖生产迁移顺序和真实数据。
- 下一段：接入图执行 Deadline/步数边界，明确异常中断 Run 的恢复与对账流程，并进行 PostgreSQL 故障注入验证。

部署前必须先在目标数据库执行 `migrations/20261117_agent_runs.sql`。API 启动会检查 `agent_runs`，缺表时拒绝启动；回滚旧版 API 并确认无运行中的 Run 后，才可删除该表。现有旧消息没有 Run 行，历史重放沿用旧消息状态。

本地原工作区含用户未提交修改，保持原样。隔离 worktree 位于 `/Users/zhangweiwei/Desktop/code/agent-gateway-m2`。本机 GitHub Git 连接不可用，PR 文件经 GitHub 连接器发布；本地与远端提交 SHA 不同，文件差异须以 PR 核对。
