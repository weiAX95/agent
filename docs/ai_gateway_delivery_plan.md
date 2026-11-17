# AI Gateway + Agent Runtime 平台：实施基线与交付计划

> 版本：v0.1（2026-11-17）  
> 代码基线：`main@5513224e9cdd50810e261e8e49025a431eef3cdf`  
> 适用范围：`weiAX95/agent`；前端另位于本地 React 工程，当前文档不假定前端已同步到 Git 仓库。  
> 规则：本页的“实现 / 验证”以证据为准，不把设计、接口声明或离线模拟视作生产验收。

## 1. 目标与非目标

建设可演进、可审计、多用户隔离的 **AI Gateway（模型入口与治理）+ Agent Runtime（受约束的 Agent 执行）**。先在既有 FastAPI 代码库内以模块化单体实现清晰边界；在指标、流量和故障隔离需求明确前，不急于拆微服务。

必须实现：模型请求身份与策略上下文、Run 生命周期、统一计量及预算闭环、跨 Worker 限流与并发、可解释模型路由、Tool/MCP 权限控制、缓存租户隔离、端到端可靠性和可观测性。

首阶段不实现：任意第三方 MCP 的无审查执行、跨租户语义缓存、未验证的自动模型降级、数据库与 Milvus 的伪跨系统事务、对不确定执行结果进行盲目重试。

## 2. 当前事实与缺口

当前 API 为 `/api/chat`，已具备 JWT、会话所有权、`request_id` 幂等、PostgreSQL 会话锁、消息持久化、SSE、模型档位选择、供应商上报用量，以及 Agent、RAG、Memory 的若干运行逻辑。`api/gateway.py` 主要负责 256 KiB 请求体上限和 Trace ID，而非完整数据面；`chat_run_events` 主要保存可见业务阶段事件，不是完整计费账本。

仓库 `offline-regression.yml` 已有成功 CI；它使用模拟模型与 SQLite，**不验证**真实 PostgreSQL/Milvus、多 Worker、模型供应商或成本扣款。RAG 有隔离服务实测，但不代替网关生产验收。

明确未就绪：统一 Run Controller、跨 Worker RPM/并发、预留与结算、provider/model 级指标、价格版本、自动健康路由、完整 MCP 沙箱、权限隔离缓存、独立控制面、事件级 SSE 续传。正式迁移与实际部署状态必须独立确认。

## 3. 责任划分

```text
Client / React
  → Edge / TLS / WAF / 请求体上限
  → FastAPI BFF（JWT 认证、会话归属、请求幂等、业务响应）
    → Agent Runtime（Run Controller、LangGraph Executor、审批、工具/RAG/Memory）
      → Gateway Data Plane（调用身份、GuardIn、RPM、并发、预算、Router、Adapter、Metering、GuardOut）
        → Model Providers
  → PostgreSQL（Run/调用/账本/事件与现有消息、checkpoint）
  → Redis（限流和临时预留；不作为最终费用真相）
Control Plane（策略版本、价目表、租户配置、凭据引用、审计）
```

- **BFF** 管理用户、会话和流式协议，不持有 provider 密钥，也不决定费用。
- **Runtime** 管理一个 Agent Run 的步骤、总 Deadline、批准点及取消。LangGraph 模型调用统一走 Gateway，Runtime 不自己重复重试整轮。
- **Gateway** 管理每次物理模型调用的路由、限流、预算、适配、错误分类和用量事件。一次 Run 可有多次模型调用；`request_id`、`run_id`、`call_id` 不能混用。
- **Control Plane** 是配置与审计来源，不应成为每次请求的同步可用性单点；数据面使用带版本的配置快照。
- **RAG** 做授权感知的语义检索/缓存，网关默认只考虑安全的 Exact Cache，不能跨用户泄露上下文。

### 标识与隔离

`tenant_id`（未来支持组织租户）、`principal_id`（从可信认证上下文取得）、`session_id`、`request_id`、`run_id` 和 `call_id` 分别承担隔离、幂等及关联职责。**不能把模型给出的 user_id 当作可信身份**；目前单用户/用户级归属应保留，添加租户维度采用向后兼容迁移。

## 4. 核心契约

### 4.1 Run 状态机

目标状态：`created → running → succeeded | failed | cancelled | timed_out`。待人工审批时先增加独立 `waiting_approval` 状态及租约、过期语义，不能直接在一个内存锁下等待人工审批。

- 只允许一次终态提交；数据库以条件更新（CAS）避免重复完成。
- Deadline 使用运行期单调时钟校验，持久记录墙上时钟截止时间，重启后采用持久截止时间重新计算，不能只依赖进程内时钟。
- `max_steps` 统计由 Runtime 明确定义的一次调度步，不得混为请求数或模型 token 数。
- Cancel 是显式终止意图；是否真正停止 provider 计费、工具副作用或记忆写入，取决于结果确认。未知结果必须记为 unknown/reconciliation，不得擅自标记为零费用。
- SSE 断线与 Run 取消不是天然等价：本版现有取消行为先保持不变；迁移时通过独立的断线策略显式控制。
- `chat_messages.status` 属于界面消息终态；新 Run 表是执行事实来源，两者经可靠映射更新，不能用两套彼此不一致的状态机。

### 4.2 计量与费用

为 **每一次物理上游调用** 写入不可变 usage event：`event_id`、`run_id`、`call_id`、provider/model、请求时间、状态、供应商 input/output/cached tokens（可空）、价格版本、币种、费用值（可空）、来源、是否估算。唯一键保证重复接收只记一次，未知用量保持 NULL，不把缺失用量等价为 0。

一次 Run 的成本为全部已确认调用事件之和；失败、取消、超时也进入旁路账本。价格按版本及生效时间计算，禁止修改历史价格后静默改写旧费用。使用整型微单位或 Decimal，禁止浮点货币结算。

### 4.3 预算与并发

`admission → reserve → invoke → record usage → settle/release`，把 **RPM/并发** 和 **金额预留/结算** 分开。Redis/Lua（或等价原子共享存储）限制跨 Worker 入口和执行；PostgreSQL 作为持久预算真相与事件账本。配额不足必须明确拒绝，存储故障执行 fail-closed 的策略；异常中断依赖 TTL + 对账工作流恢复预留，不可无条件重复扣费。取消后仍须处理迟到的用量回执。

### 4.4 模型路由与安全

- 先支持手动档位和规则路由，再引入健康探测、熔断、候选模型兼容性分组及限次降级；流式已输出 token 后默认禁止透明重试/切换。
- Guardrails：输入大小/敏感数据/策略检测，输出在流式场景区分缓冲策略与实时策略；明确审计原因。
- Tools/MCP：可信身份注入、租户许可、工具白名单、参数/URL 校验、最小权限、出站代理、超时、输出上限；写操作审批和幂等必须独立处理。
- 缓存 Key 必须绑定 tenant、授权范围、模型与配置版本、工具上下文；默认不在网关做跨用户 Semantic Cache。

## 5. 交付顺序与验收门槛

以下阶段按依赖推进，未写明“通过”的项目均不视作完成。每一阶段在独立 PR 中附设计变更、中文注释、迁移/回滚、单元与集成测试证据、风险记录。

| 阶段 | 优先级 | 产物 | 合并门槛 |
| --- | --- | --- | --- |
| M0 基线冻结 | P0 | 当前事实、风险、回归命令、模块责任 | main SHA 与 CI 记录一致；所有未知项公开 |
| M1 Run 领域契约 | P0 | `src/agent/run_policy.py`、严格状态/Deadline/步数规则、离线测试 | 正常、超限、超时、终态不可重复均有通过的 CI；**尚不对线上生效** |
| M2 Run 持久化与接入 | P0 | `agent_runs` 表、CAS、关联 `chat_messages`、Run Controller 接入 /chat | 真实 PostgreSQL 同时提交/取消/进程崩溃/恢复验证，不重复调用副作用工具 |
| M3 Usage ledger | P0 | `gateway_model_calls`、`usage_events`、价格版本、关联 trace | 多次模型调用全覆盖，失败/取消/迟到数据不丢；幂等与金额准确性测试 |
| M4 限流/预算 | P0 | Redis 原子 RPM+并发、预算预留/结算/对账 | 多 Worker 压测，不超发预算，不泄漏并发令牌，故障策略明确 |
| M5 路由可靠性 | P1 | Router、Adapter、Circuit Breaker、健康度与可观测 | 故障注入及兼容性验证；无副作用重复执行 |
| M6 Tool/MCP 安全 | P1 | 授权上下文、白名单、超时、沙箱、审批与审计 | 跨租户/SSRF/提示注入/取消/审批拒绝的负向测试 |
| M7 缓存、SSE 与 Guardrails | P1 | Exact Cache 隔离、事件续传、输入输出保护 | 并发重放、权限变更、断点去重及隔离验收 |
| M8 控制面及上线 | P2 | 版本化配置、密钥引用、Admin API、仪表盘和部署脚本 | 真实外部服务端到端、备份恢复、告警、限流/费用压测、回滚演练 |

依赖次序：M1 → M2 → M3 → M4 → M5/M6 → M7/M8。M6 的基础安全修补可提前阻断严重风险，不受顺序限制。

## 6. 工程约束

1. **代码注释使用中文**：新增/修改的关键类、函数、复杂状态迁移、并发边界和失败兜底必须有中文解释；标识符、协议字段和日志结构保留行业通用英文，不添加无意义逐行注释。
2. 模块按职责渐进重构：优先复用现有 `api/` 与 `src/agent/`，在契约稳定前不直接拆新服务；所有现有 `/api/chat` 协议行为以回归保护。
3. 每个改动有明确测试，不触发真实付费模型的 CI 测试不得标榜“生产验证”。
4. 迁移必须前向兼容、可重复执行、有回滚方案；不能以 `Base.metadata.create_all` 代替已有表的正式 schema migration。
5. 日志与指标禁止记录密钥、原始用户内容或高基数原文；trace 与账本访问必须认证鉴权。
6. 每个阶段记录：代码 HEAD、已实现、已验证、风险、阻塞、下一步；周报沿用同一口径。

## 7. 当前迭代（M1）

本分支提供 **纯领域 Run 控制策略和测试**：`RunLimits`、`RunController`、`RunStatus`。其职责只是在单次执行中限制步数和单调时钟 Deadline，并约束不可逆终态；不处理数据库事务、跨进程锁、HTTP/SSE 或实际 token/费用预算。

完成定义：
- [x] 领域契约及基本异常代码设计
- [x] 独立单元测试加入离线 CI 配置
- [ ] 新提交的 CI 通过（以实际检查结论为准）
- [ ] M2 持久状态表与事务接入
- [ ] 实际 Agent Graph 中启用限制
- [ ] 真实端到端与故障注入验证

**不要把 M1 的纯领域模块当作完整 Run Controller 已上线。**

## 8. 需要持续核对的风险

- 既有 PostgreSQL checkpoint 与消息分别提交，部分执行可能已写 checkpoint 却未完成消息事务，恢复时不可盲目重放。
- 现有 `UsageMetadataCallbackHandler` 的聚合结果不一定包含所有被调用 provider 的精确用量。
- 当前 SSE 只支持终态重放，缺每条事件 ID 与 `Last-Event-ID`；不能宣称 token 级断点续传。
- 当前多模型档位是**手动配置/选择**，不等价于策略路由。
- 私有本地工作区可能包含尚未推送的变动；本分支基于远端 main，合并/应用之前应先对照本地 `git status` 和 diff。

参考：`docs/ai_gateway_current.md`、`docs/gateway_evaluation_roadmap.md`、`docs/session_current.md`、`docs/rag/verification.md`。
