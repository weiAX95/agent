# 本轮目标：新会话存储后端

开始日期：2026-10-26。状态：本轮后端目标已完成（不代表完整 V0.1 已完成）。

## 用户结果

登录用户能够创建自己的会话，在同一会话发送多轮消息。消息在开始生成前可持久化，完成、停止和失败后有明确状态；重复请求不重复调用模型，其他用户不能使用该会话。

## 范围

1. ORM 对齐已创建的 chat_sessions、chat_messages。
2. POST /api/sessions 创建空会话，归属从 JWT 取得。
3. /api/chat 接入消息占位、序号、请求幂等、最终正文/状态以及旧 ChatLog 反馈兼容。
4. 保持同会话互斥，验证跨提交的锁安全、断线和异常清理。
5. 补充回归与隔离 PostgreSQL 集成验证，归档修改清单与结果。

不实现历史列表界面、旧数据回填、删除/重命名或模型效果优化；不提交或推送 Git。

## API 决策

新聊天调用需要先创建会话，随后发送 session_id、message、request_id。request_id 一次生成并在网络重发时复用；显式重新生成使用新 ID。接口将拒绝不存在、已删除或不属于当前用户的会话。旧前端会在后续接入；本轮不使用隐式创建来绕过会话归属检查。

## 验收清单

- [x] S1 ORM 与实际数据库字段、默认值、约束一致。
- [x] S2 创建会话返回 ID，owner_id 不由客户端指定。
- [x] S3 生成前提交 user(completed) 与 assistant(streaming)，序号唯一。
- [x] S4 正常完成同时提交 assistant 与兼容 ChatLog，提交后才发 done。
- [x] S5 停止/失败持久化真实状态；失败不生成成功 ChatLog。
- [x] S6 重复 request_id 相同正文重放已存结果，不重新调用模型；不同正文返回冲突。
- [x] S7 同会话互斥跨业务事务生效；其他会话可并行；取消/异常释放锁。
- [x] S8 越权、软删除和不存在会话在调用模型前拒绝。
- [x] S9 断线/进程遗留 streaming 不盲目重跑，以 interrupted 明示需处理的状态。
- [x] S10 现有回归、真实隔离库验证及修改清单已完成。

## S5–S9 对应代码

以下位置对应当前后端实现；`ChatLog` 是供旧反馈接口使用的成功问答日志，完整消息状态记录在 `chat_messages`。

| 步骤 | 修改位置 | 实际修改与生效方式 | 核对位置 |
| --- | --- | --- | --- |
| S5 停止/失败 | [api/chat_storage.py](../api/chat_storage.py#L103-L158) `ChatTurn.finish()`；[api/chat_stream.py](../api/chat_stream.py#L69-L111) `produce()` | 部分回复定期提交；终态将助手消息从 `streaming` 更新为 `completed`、`stopped`、`failed` 或 `interrupted`，记录正文、错误码和完成时间。只有 `completed/stopped` 才写兼容 `ChatLog`，并在提交成功后发送 `done/stopped`；模型失败发 `GENERATION_FAILED`，保存失败发 `SAVE_FAILED`，不发假成功终态。 | [tests/test_chat_persistence.py](../tests/test_chat_persistence.py#L52-L72) 失败无日志；[同文件](../tests/test_chat_persistence.py#L88-L121) 停止及保存失败 |
| S6 请求幂等 | [api/models.py](../api/models.py#L67-L93) 唯一约束；[api/chat_storage.py](../api/chat_storage.py#L178-L199) `begin_turn()`；[api/chat_stream.py](../api/chat_stream.py#L37-L52) `replay()` | `(session_id, request_id, role)` 唯一。同一请求先核对原 user 正文：正文不同返回 409；相同且 assistant 已终态时返回已存记录，`persisted_chat_response()` 只发重放事件，不再进入调用模型的 `stream()`。显式重新生成需换 `request_id`。 | [tests/test_chat_storage.py](../tests/test_chat_storage.py#L72-L101)；[tests/test_chat_persistence.py](../tests/test_chat_persistence.py#L74-L86) |
| S7 同会话互斥 | [api/chat_storage.py](../api/chat_storage.py#L46-L73) `session_execution_lock()`；[api/server.py](../api/server.py#L75-L96) 独立锁连接池；[api/server.py](../api/server.py#L119-L161) 请求级依赖 | 用用户与会话计算 PostgreSQL 会话级 advisory lock。锁由独立连接持有，消息占位和终态提交不会释放它；同会话竞争返回 409，不同会话使用不同锁键。SSE 结束、取消和异常经 `finally` 解锁；锁状态不明或解锁失败时关闭连接，避免带锁回池。 | [tests/test_chat_api.py](../tests/test_chat_api.py#L60-L163)；[scripts/verify/chat_storage_postgres.py](../scripts/verify/chat_storage_postgres.py#L69-L81) |
| S8 越权/删除/不存在 | [api/chat_storage.py](../api/chat_storage.py#L31-L43) `owned_session()`；[api/server.py](../api/server.py#L119-L136) `get_chat_turn()` | 查询同时限定 `id`、认证得到的 `owner_id` 和 `deleted_at IS NULL`。入口在获取锁和调用模型前先检查一次，`begin_turn()` 内再次检查；不符合统一返回 404，不泄露其他用户会话。 | [tests/test_chat_storage.py](../tests/test_chat_storage.py#L51-L60)；[tests/test_chat_api.py](../tests/test_chat_api.py#L35-L50) |
| S9 断线/遗留状态 | [api/chat_stream.py](../api/chat_stream.py#L80-L129) 流关闭清理；[api/server.py](../api/server.py#L138-L150) 请求结束兜底；[api/chat_storage.py](../api/chat_storage.py#L160-L175) 与 [同文件](../api/chat_storage.py#L201-L222) | 断线会取消生产任务；模型若确认停止则保存 `stopped`，取消向上传播或状态不明则保存 `interrupted/CHECKPOINT_RECOVERY_REQUIRED`。若进程退出留下 `streaming`，下一次持锁请求检测后改为 `interrupted` 并返回 409，拒绝盲目重新执行。这里是下次请求时发现，并非后台自动修复；checkpoint 与消息表未做自动对账。 | [tests/test_chat_storage.py](../tests/test_chat_storage.py#L110-L119)；[tests/test_chat_api.py](../tests/test_chat_api.py#L94-L122)；[tests/test_chat_persistence.py](../tests/test_chat_persistence.py#L123-L144) |

## 修改地图（完成后补充证据）

| 文件 | 目的 |
| --- | --- |
| api/models.py | 两张新表的 ORM |
| api/routers/sessions.py | 创建会话接口 |
| api/chat_storage.py | 归属、互斥、幂等与消息写入 |
| api/chat_stream.py | 流式执行与最终状态处理 |
| api/server.py | 注册接口与接入新聊天流程 |
| tests/test_chat_storage.py | 数据与权限测试 |
| tests/test_chat_persistence.py | 流式接口及故障回归 |
| docs/chat_session_schema.md / README.md | 合约与已验证边界 |

验收报告：[修改地图与验收说明](../docs/session_storage_delivery.md)。十组离线测试合计 76 项通过；真实 PostgreSQL 独立 schema 验证了锁、两轮存储、真实路由/JWT/反馈与 checkpoint 连接池重建后的恢复。只替换模型，不调用真实模型/Milvus。总体 V0.1 仍包含历史界面、真实模型/Milvus 验收和连续自用，不能将本轮完成等同 V0.1 完成。
