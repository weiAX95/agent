# AI Gateway M4 进度

## 当前进度：预算领域契约与 Redis 准入

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

### 尚待实现及验收

1. 把 Redis 准入接到 Run 启动与终态释放，添加真实多进程压测和服务故障恢复验证。
2. PostgreSQL 预算账户、预留及结算迁移；事务锁/CAS、幂等、迟到回执、人工对账和回滚演练。
3. 在模型发送前完成准入与预留，终态释放并发租约，费用按物理调用结算；保持 `/api/chat` 与 SSE 协议兼容。
4. 多 Worker 压测、崩溃恢复、真实 PostgreSQL/Redis 集成与目标环境验收。

本切片是 M4 的起点，M4 阶段未完成。
