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
