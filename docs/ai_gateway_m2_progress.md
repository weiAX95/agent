# AI Gateway 开发进度（2026-11-18）

## M1

- 已实现：Run 领域状态、步数与 Deadline 规则；PR #2 已合并到 `main@9a34458b`。
- 已验证：Python 3.12 本地 8 个测试通过；PR #2 的 GitHub Actions `Offline regression tests` run `44026651631` 成功。
- 边界：纯进程内策略，尚未接入实际 Agent 执行。

## M2 当前进度

- 已实现：`agent_runs` ORM 与前向 SQL 迁移、Run 创建参数、数据库 CAS 状态转换。`/api/chat` 的消息预留与 Run 创建共用事务；模型执行前启动 Run；正常成功、失败或停止时，消息与 Run 终态共用事务。重复提交消息终态现在显式报冲突，不能伪装成成功。持久截止时间换算为单调时钟剩余时间，主 Agent 图使用 LangGraph 递归上限限制调度步，研究子图共享剩余步数，并将已完成步数 CAS 写回 Run。异常中断或失联后在同一事务标记 `recovery_required`；迟到的启动、步数和自动终态提交均被 CAS 拒绝。新增只读恢复核对快照及放弃裁决命令：只暴露执行元数据，不返回原始消息或工具输出；核对外部结果后，以会话锁、checkpoint 标识和 CAS 确认状态，再将 Run 失败终态与审计记录同事务提交。旧会话保持阻断。
- 已验证：Python 3.12 本地相关离线测试 39 个通过，完整 CI 以远端检查为准。过期 Run 不调用模型；运行中超时提交 `timed_out`；真实 LangGraph 循环在上限后未再次安排工具。嵌套 LangGraph 测试确认研究子图在配额耗尽前不安排下一次检索或父图模型调用；模拟工具节点把持久化异常转为消息后，Run 仍会失败。一次性 PostgreSQL 18 容器专项 6 个通过，覆盖旧版会话与消息保留、迁移重复执行、新表移除后的旧数据读取、双连接终态和步数 CAS 竞争、断开连接后读取及请求唯一键；两个独立连接竞争完成/取消时，只有一个事务提交且消息、Run 和日志一致。子进程在真实 LangGraph checkpoint 落盘前向独立本地 HTTP 接收端提交一次工具副作用，随后强制退出；重启后运维命令读取 checkpoint 元数据，错误标识拒绝裁决，正确证据让 Run 与审计记录同事务提交，重复裁决仍被拒绝；旧会话继续返回 409，HTTP 接收端仍只记录一次。
- 待验证：真实第三方工具回执的对账和生产运维身份/权限；当前裁决命令只对持有数据库凭据的运维人员开放，本地 HTTP 故障注入不代表第三方生产验收。只读快照和放弃裁决均不会恢复旧会话。数据库状态封锁不能撤销已发出的模型调用或工具副作用。另需目标生产库与旧版 API 的迁移/回滚演练。本次旧数据测试只验证一次性数据库中的 schema 兼容性。模型不响应取消时，Deadline 只能在模型返回或可取消边界落地。
- 下一段：定义人工核对后的恢复操作与审计证据，验证真实外部工具的故障行为。

部署前必须先在目标数据库执行 `migrations/20261117_agent_runs.sql`。API 启动会检查 `agent_runs` 与 `agent_run_reconciliations`，缺表时拒绝启动；回滚旧版 API 并确认无运行中的 Run 后，先删除裁决表，再删除 Run 表。现有旧消息没有 Run 行，历史重放沿用旧消息状态。

## 待核对 Run 的运维放弃操作

仅在已取得目标数据库授权、确认 Worker 不再执行，并向外部系统核对工具副作用后使用。命令要求 `DATABASE_URL` 环境变量；证据引用应指向工单或外部回执，不填聊天原文或工具输出。

```sh
python -m tools.inspect_run --run-id <RUN_UUID> --owner-id <OWNER_UUID>
python -m tools.abandon_run --run-id <RUN_UUID> --owner-id <OWNER_UUID> \
  --reviewer-id <REVIEWER_UUID> --checkpoint-id <CHECKPOINT_ID> \
  --external-effect-status confirmed --evidence-ref <EVIDENCE_REF> --confirm-abandon
```

确认没有外部副作用时将状态设为 `none`；无 checkpoint 时以 `--no-checkpoint` 替代 `--checkpoint-id`。状态未知时不能执行裁决。命令会检查同会话 advisory lock、checkpoint 是否变化及待写任务，成功后记录 `agent_run_reconciliations`，将 Run 设为 `failed`，但不清除旧会话的中断标志，也不重放工具。重复裁决会被拒绝。

本地原工作区含用户未提交修改，保持原样。隔离 worktree 位于 `/Users/zhangweiwei/Desktop/code/agent-gateway-m2`。本机 GitHub Git 连接不可用，PR 文件经 GitHub 连接器发布；本地与远端提交 SHA 不同，文件差异须以 PR 核对。
