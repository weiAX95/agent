# AI Gateway 开发进度（2026-11-17）

## M1

- 已实现：Run 领域状态、步数与 Deadline 规则；PR #2 已合并到 `main@9a34458b`。
- 已验证：Python 3.12 本地 8 个测试通过；PR #2 的 GitHub Actions `Offline regression tests` run `44026651631` 成功。
- 边界：纯进程内策略，尚未接入实际 Agent 执行。

## M2 第一段

- 已实现：`agent_runs` ORM 与前向 SQL 迁移、Run 创建参数、数据库 CAS 状态转换。
- 已验证：离线 SQLite 相关测试共 10 个通过；重复终态和非法转换被拒绝。
- 待验证：真实 PostgreSQL 并发、迁移、崩溃恢复；`/api/chat` 消息事务与 Run 状态尚未接入。
- 下一段：在消息预留事务创建 Run，执行前启动 Run，终态与消息同事务提交，并接入 Agent 调度边界。

本地原工作区含用户未提交修改，保持原样。隔离 worktree 位于 `/Users/zhangweiwei/Desktop/code/agent-gateway-m2`。本机 GitHub Git 连接不可用，PR 文件经 GitHub 连接器发布；本地与远端提交 SHA 不同，文件差异须以 PR 核对。
