# AI Gateway M4 进度

## 当前进度：预算领域契约、Redis 准入与请求入口接线

从已合并 PR #5/#6 的 `main` 开始。`src/agent/budget_policy.py` 定义同一币种、同一预算周期的 `limit / spent / reserved` 快照及预留、结算规则。金额使用 Decimal，精度与现有账本的 `Numeric(30,12)` 对齐。

### 已实现

- 新预留必须计入已有花费和未结预留；超过额度时返回稳定错误码 `GATEWAY_BUDGET_EXCEEDED`。
- 供应商用量未知时保留预留，不把未知费用释放为零。
- 已确认费用即使高于预留也如实计入花费；后续准入自然被阻断。
- 拒绝浮点、负数、超精度金额和不规范币种。

### 已验证

`test_budget_policy.py` 离线测试覆盖上述边界。该测试证明领域运算，不证明共享存储原子性或请求链实际接入。

### Redis 准入切片

- `api/redis_admission.py` 用单个 Lua 脚本原子检查滑动 60 秒 RPM 和未过期的并发租约。两个键使用同一用户 hash tag，避免 Redis Cluster 多键脚本跨 slot；Redis 服务端时间决定窗口与租约过期。脚本依据 Redis 官方[原子执行语义](https://redis.io/docs/latest/interact/programmability/eval-intro/)设计。
- 同一 Run 在租约仍有效时重复申请不增加 RPM；租约释放后、RPM 窗口内再次申请被拒绝。终态释放只删除租约，RPM 留到窗口结束。Redis 错误统一拒绝新准入，租约释放故障由 TTL 回收。
- 本地一次性 Redis 7 容器验证两独立客户端并发申请、RPM 拒绝、重复申请、释放幂等及租约过期；离线测试验证参数与故障拒绝。CI 在无 Redis 时运行离线测试，真实 Redis 集成仍需在目标环境重跑。

### PostgreSQL 预算预留切片

- 迁移 `20261124_gateway_budget_reservations.sql` 新增按用户、UTC 日期、币种唯一的预算账户，以及每个 Run 唯一的预留记录。服务器启动时不使用 `create_all` 隐式创建这两张正式迁移表。
- `reserve_run_budget` 在同一事务中先锁 Run、再锁账户；重复的相同 Run 返回原预留，参数变化则拒绝。账户缺失、跨用户和额度不足均拒绝。现阶段账户需预先配置，尚无控制面写入接口。
- SQLite 离线测试覆盖归属、幂等、账户缺失和超额；一次性 PostgreSQL 18 测试覆盖迁移重放、两条 Run 并发争抢额度、同一 Run 并发重试。本地测试库名固定为 `gateway_m4_test`，未触碰目标数据库。目标环境正式迁移尚未验证。

### PostgreSQL 结算与迟到修正切片

- `settle_run_budget_from_usage` 以 Run 和预算账户行锁串行化结算，从 M3 不可变用量事件中选每次物理调用最新的已确认回执。Run 未到终态或任一调用费用未知时保留整笔预留；币种不匹配拒绝结算。
- 全部费用已确认时，将预留释放并按真实费用累计花费。相同 Run 重试不重复扣费；迟到回执更正仅调整差额，每次变动追加 `gateway_budget_adjustments` 审计记录。异步 Agent 与同步 Memory 两条物理调用路径均拒绝终态 Run 再发送；两条回执路径与结算使用 Run 行锁串行化。
- SQLite 离线测试覆盖未知费用保留、取消后的迟到回执、重复结算及费用增减修正。一次性 PostgreSQL 18 测试覆盖同一 Run 两个事务并发结算且只追加一条变动记录；M3 物理调用与同步 Memory 路径的 PostgreSQL 回归也通过。现有聊天与用量测试一并通过。目标环境正式迁移仍待验证。

### `/api/chat` 入口接线切片

- 仅当 `GATEWAY_ADMISSION_ENABLED=true` 时启用 M4。启动前需配置 `GATEWAY_REDIS_URL`、`GATEWAY_RPM_LIMIT`、`GATEWAY_CONCURRENT_LIMIT`、`GATEWAY_BUDGET_CURRENCY`、`GATEWAY_RESERVATION_AMOUNT`，执行 M4 迁移并预建相应用户当日预算账户；缺少配置、迁移或 Redis 连接时启动失败。
- 新 Run 在模型发送前依次申请 Redis 租约和 PostgreSQL 预算预留；预算失败会回滚并释放租约。终态尝试结算并释放并发租约，未知费用保留预算预留，异常中断依赖租约 TTL。SSE 请求重放保留稳定的网关拒绝错误码和中文消息。
- HTTP 离线测试覆盖准入先于模型、拒绝后不调用模型及错误重放；预算存储测试覆盖成功结算和预留失败释放。一次性 PostgreSQL 18 + Redis 7 组合测试通过申请、预留、终态结算和释放；Redis 独立测试覆盖双客户端共享并发、RPM 与过期。
- 当前配置为所有用户使用同一组限额参数，正式账户额度需外部预建。尚未在真实服务启动和多 Worker 压测中验收，也没有自动迟到回执对账任务。

### 尚待实现及验收

1. 添加自动迟到回执对账任务与人工核对入口，补齐回滚演练；预算账户写入控制面或部署配置。
2. 多 Worker 压测、崩溃恢复和租约续期策略；验证长时 Run 与 TTL 的边界。
3. 在实际 API 服务与模型路径做端到端验收，确认同步 Memory、异步 Agent、SSE 和现有 RAG 能力兼容。
4. 目标环境执行正式迁移、配置账户和 Redis，再验证故障拒绝及费用账本与预算账户对齐。

本切片是 M4 的起点，M4 阶段未完成。
