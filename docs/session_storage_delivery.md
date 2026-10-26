# 新会话存储后端：目标、改动与验收

日期：2026-10-26。目标文件：[session_storage_goal.md](../tasks/session_storage_goal.md)。

## 这次用户能得到什么

通过 API 创建属于自己的新会话，再以固定 session_id 连续发送。每轮开始前保存用户消息及助手占位；生成完成、用户停止、模型失败后有明确记录。同一 request_id 网络重发不重复调用模型。同会话正在生成时，第二个请求收到 409。

本轮未做前端、会话列表、历史读取接口、旧数据导入或人工恢复工具。旧前端仍只发送 session_id/message，必须在后续接入创建会话和 request_id 后才能使用新 /chat；旧请求会返回 422 或 404，不隐式创建或绕过归属检查。

## 修改地图

| 文件 | 为什么改 | 如何核对 |
| --- | --- | --- |
| api/base.py、api/db.py | 单独保存 ORM metadata，模型与测试不用导入配置或连接真实数据库 | db.py 仍导出同一个 Base；测试从空库创建全部模型 |
| api/models.py | 新增 ChatSession / ChatMessage，与已建两表对应 | 字段、外键、唯一约束、状态 CHECK、列表索引 |
| api/routers/sessions.py | 新建 POST /api/sessions，owner 从现有 JWT 取得 | 201 返回 ID/标题/时间；传 owner_id 或非法标题拒绝 |
| api/chat_storage.py | 集中会话归属、互斥、请求幂等、消息占位和最终事务 | begin_turn、finish、session_execution_lock |
| api/chat_stream.py | 从 server.py 移出 SSE 编排，先持久化再发终态 | completed/stopped/failed/interrupted、取消、保存失败、重放 |
| api/server.py | 注册会话路由，接入新流程和独立的锁连接池 | get_chat_turn 依赖覆盖整个响应；finally 清理和解锁 |
| tests/test_chat_storage.py | 使用实际 ORM 的离线存储验证 | 创建、归属、两轮排序、重复键、改正文、孤儿消息与事务失败 |
| tests/test_chat_persistence.py | 验证流式用户结果及数据库状态 | done 前落库、失败保存、停止、断线、重复重放、异常取消 |
| tests/test_chat_api.py、tests/chat_api_fixture.py | 真实 FastAPI 请求/依赖生命周期；替换外部模型与锁后端 | 401/404/409/422、并发、断线后锁释放 |
| tests/test_checkpoint_flow.py、tests/test_stream_recovery.py | 原来只验证 ChatLog 的两条 API 测试适配新消息链路 | 保留提交后发 done、失败不写成功日志、重试后提交的验证 |
| scripts/verify/chat_storage_postgres.py | 显式运行真实 PostgreSQL 集成验收，使用随机临时 schema | 独立锁连接池、ORM、真实应用/JWT、反馈及真实 checkpoint |
| .github/workflows/offline-regression.yml | 自动运行新增测试 | 增加 FastAPI/aiosqlite 最小依赖；不调用真实模型/数据库 |

## 写入行为前后对比

旧流程：获取事务锁 → 调模型 → 回答结束才写一条 ChatLog → 提交 → 发 done。用户消息和助手状态在生成过程中没有独立记录。

新流程：

```text
JWT 用户 + 已创建的 session_id + request_id
  → 验证归属及未删除
  → 独立连接获取会话执行锁（其他同会话请求返回 409）
  → 查 request_id：已终态则重放；同键不同正文返回 409
  → 提交 user(completed) 与 assistant(streaming)
  → 调用 Agent、发送 token，最多每秒写一次部分正文
  → 短事务：更新助手终态 + 更新时间 + 成功/停止时的 ChatLog
  → 提交成功后发 done/stopped；失败发 error
  → 请求结束清理遗留 streaming，释放执行锁
```

- 锁连接池与 checkpoint 连接池分开，防止并发聊天占满锁连接后，checkpoint 无连接可用。
- 新轮次失败保留 user/assistant 记录；不生成成功 ChatLog。明确重试用新的 request_id。
- 模型结果已经生成但最终落库失败，按 interrupted/待核对处理，不发假 done，也不盲目重跑。
- 检测到遗留 streaming 时，在持锁确认没有正常 worker 执行后标记 interrupted，阻止此会话继续盲跑；首版建议新建会话，人工核对/恢复工具后续实现。
- 模型异常中断直接传播 CancelledError 时也发送错误终态，避免客户端一直等待。
- UI 消息与 LangGraph checkpoint 使用不同事务，并非跨连接原子提交。本轮提供请求关联及异常阻断，不宣称实现自动分布式补偿。
- 结束时旧 ChatLog 使用助手消息 UUID 作为日志 ID，done/stopped 保持 chat_log_id，原反馈接口可继续使用。

## 调用合约

先注册/登录，沿用原来的 Bearer access token。不要将真实 token 写入文档或提交到 Git。

```http
POST /api/sessions
Authorization: Bearer <access_token>
Content-Type: application/json

{"title":"Agent 学习"}
```

返回示例：

```json
{"code":0,"data":{"id":"<服务端生成的会话 UUID>","title":"Agent 学习","created_at":"<时间>"},"message":"ok"}
```

然后发送：

```http
POST /chat
Authorization: Bearer <access_token>
Content-Type: application/json
Accept: text/event-stream

{"session_id":"<上一步返回的 UUID>","request_id":"<本次请求的 UUID>","message":"你好"}
```

- 继续同一会话：沿用 session_id，每条新问题生成新的 request_id。
- 同一请求因网络问题重发：沿用 request_id 及完全相同的 message。
- 生成中重发：409；已完成重发：SSE done，replayed=true，未再次调用模型、不重复统计 usage。
- 同键不同正文：409；无效 JSON/UUID/缺字段/纯空白消息：422；无身份：401；会话不存在、已删除或无权限：404。
- 失败请求重发同一 ID 只重放失败，不重新生成。显式重试生成时换 request_id。
- 创建空会话接口暂不提供创建请求幂等；重复创建可能产生多个空会话，本轮未实现列表且不承诺该层去重。

## 验证范围

离线：CI 原有七组 + 存储/流式/HTTP 三组，使用真实 ORM 的隔离 SQLite、模拟模型和锁后端。

真实 PostgreSQL：运行 `.venv/bin/python -m scripts.verify.chat_storage_postgres`。脚本读取本地连接配置，创建随机临时 schema；运行真实 ORM、PostgreSQL advisory lock、LangGraph PostgresSaver、应用实际路由与 JWT 校验；只替换模型提供方。验证同会话互斥跨提交有效、不同会话可并行、两轮历史4条、重放、冲突、隔离、旧反馈和连接池重建后的恢复。最后清理临时 schema，不修改 public 数据。

不包含真实大模型/Milvus效果验证、前端浏览器体验、长时间压力测试或整个进程崩溃恢复演练。尚不能将本轮标记为完整 V0.1 交付。

## 本轮验证结果

- 十组离线测试：76 项全部通过；无跳过或关闭既有验证。结果汇总在本地忽略目录 output/session-storage/offline-results.json。
- 真实 PostgreSQL 临时 schema：互斥跨提交、不同会话、两轮消息、重放/冲突/隔离、实际注册路由与 JWT、反馈兼容、checkpoint 连接池重建均通过；临时 schema 已清理。
- 故障分支：断线后等待模型停止结果并保存；锁获取返回丢失/解锁失败会关闭连接再交回连接池。
- 权限测试变异验证：在内存中反转 owner_id 条件会使隔离测试失败，未改动磁盘实现。
- Python 编译与 git diff --check 通过。未调用真实模型或 Milvus，未提交或推送 Git。

建议阅读顺序：api/models.py → api/routers/sessions.py → api/server.py 的 get_chat_turn → api/chat_storage.py 的 begin_turn/finish → api/chat_stream.py。原有 web/ 两个文件的删除以及其他本地未跟踪工具在本轮开始时已存在，不属于本轮改动。
