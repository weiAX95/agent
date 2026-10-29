# LLM Agent

[English](README.en.md) · [项目进度](docs/TODO.md) · [功能改动时间线](docs/change_history.md)

基于 FastAPI、LangChain / LangGraph 的聊天 Agent 实验项目，探索流式响应、PostgreSQL 会话恢复与 Milvus 长期记忆。

> **状态：实验阶段。** 已有离线回归测试和部分集成验证记录；真实模型、Milvus 与完整聊天接口的端到端联调尚未完成。当前不作为可直接对外部署的成品。

## 项目关注的问题

| 问题 | 当前实现 |
| --- | --- |
| 服务重启后的上下文恢复 | PostgreSQL checkpoint |
| 不同用户与会话的历史隔离 | 稳定的复合会话标识 |
| 同一会话的并发请求 | PostgreSQL session advisory lock；同会话冲突返回 409 |
| 流式回复与失败恢复 | SSE 事件与定向离线回归 |
| 长期记忆保存与召回 | Milvus 与本地嵌入模型 |

## 快速开始

```bash
git clone https://github.com/weiAX95/agent.git
cd agent
```

从项目根目录运行。建议使用 Python 3.12；先准备 PostgreSQL、Milvus（默认 `localhost:19530`）以及本地嵌入模型 `~/models/bge-base-zh-v1.5`。当前工具模块在导入时会连接 Milvus 并加载嵌入模型，因此这些依赖不可用时，API 可能无法启动。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

在项目根目录创建 `.env`，至少配置以下项目。`DATABASE_URL` 是 SQLAlchemy 使用的 asyncpg URL；[checkpoint.py](src/agent/checkpoint.py) 会将它转换为 psycopg 可用的连接串，复用同一个 PostgreSQL 数据库。数据库账号须有首次创建 checkpoint 表的权限。不要将真实密钥提交到 Git。

```dotenv
DATABASE_URL=postgresql+asyncpg://agent:your_password@localhost:5432/agent
OPENAI_API_KEY=your_key
OPENAI_BASE_URL=https://your-model-api.example/v1
OPENAI_MODEL=your_model
```

认证功能还要求以下配置。它使用 HS256，签名和验证密钥应为同一个高熵随机值；真实密钥仅放在未提交的本地配置中。

```dotenv
JWT_SIGNING_KEY=replace_with_a_random_secret
JWT_VERIFYING_KEY=replace_with_the_same_random_secret
JWT_ISSUER=agent-api
JWT_AUDIENCE=agent-web
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=15
```

缺少这些 JWT 配置时 API 无法正常启动。

启动 API：

```bash
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000 --reload
```

首次启动会初始化 API 使用的 SQLAlchemy 表和 LangGraph checkpoint 表；初始化过程由 PostgreSQL advisory lock 串行化，避免多个 API worker 同时执行迁移。检查数据库连接，再发送聊天请求：

```bash
curl http://127.0.0.1:8000/health
```

`/api/chat` 需要先注册或登录，再用返回的 access token 请求：

```bash
curl -X POST http://127.0.0.1:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"replace_with_a_strong_password"}'

# 从注册响应的 access_token 复制令牌；已有账号可调用 /api/auth/login。
ACCESS_TOKEN='paste_access_token_here'
# 先创建会话，复制响应 data.id；标题可省略。
curl -X POST http://127.0.0.1:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -d '{"title":"首次对话"}'
SESSION_ID='paste_session_uuid_here'
# 每条新消息使用新 request_id；同一请求网络重发时复用它。
REQUEST_ID=$(python -c 'import uuid; print(uuid.uuid4())')
curl -N -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -d "{\"session_id\":\"${SESSION_ID}\",\"request_id\":\"${REQUEST_ID}\",\"message\":\"你好\"}"
```

## 工程细节

`/api/chat` 使用 SSE 返回 `token`、`usage`、`done`、`stopped` 或 `error` 事件。`usage` 是模型供应商经 LangChain 返回的真实 token 统计；供应商未返回时标记不可用，不按字符数伪造估算。前端以 `fetch` 发送 POST JSON 并读取响应流；浏览器原生 `EventSource` 不支持这里使用的 POST 请求体。收到 `done` 时，`data.answer` 是完整回复。[main.py](src/agent/main.py) 用 `make_thread_id(user_id, session_id)` 生成稳定且无歧义的 checkpoint 线程 ID；同一用户、同一会话会继续之前的短期历史，不同用户复用相同 `session_id` 也不会意外共用线程。API 启动时创建 PostgreSQL 连接池和 Agent，关闭时释放连接。每轮只提交当前用户消息；召回内容通过动态系统提示传入，不累积到持久化消息历史中。Agent 回复和工具消息仍由 checkpoint 保存。`ChatLog` 只记录问答，不承担 checkpoint 恢复状态的工作。

`/api/chat` 先验证会话归属，再通过独立连接池持有 session advisory lock，覆盖消息占位提交、模型执行和最终结果写入。同一会话并发请求返回 409，不排队盲跑；不同会话使用不同锁键。每轮先提交用户消息和生成中的助手消息，结束后更新状态并兼容写入 `ChatLog`，提交成功后才发终态。请求幂等依靠 `request_id`：相同正文重放终态，同键不同正文返回 409。详情与修改地图见 [新会话存储交付](docs/session_storage_delivery.md)。

正式聊天流程由 [请求级业务图](src/agent/chat_workflow.py) 编排召回、回答和长期记忆维护；图本身不另存 checkpoint。同步调用与 POST SSE 接口共享这些阶段，流式 token、断线停止和失败清理仍由原有 Agent 流处理。回答后的记忆维护失败会记录服务端异常并保留回答；节点与验证范围见 [迁移记录](docs/chat_graph_migration.md)。

**调用合约变化（2026-10-26）**：`/api/chat` 现在要求先创建会话并提供 `request_id`。现有 React 前端尚未适配，直接发送旧请求会被拒绝；本轮仅交付后端与接口验证。

独立的 React 前端位于 `/Users/zhangweiwei/Desktop/code/web/my-app`。新版本的 `done` / `stopped` 事件附带 `chat_log_id`；页面可将回答问题或记忆问题提交到 `POST /api/feedback`，由服务端按当前用户验证记录归属。人工脱敏、回归样本提升及基线对比命令见 [反馈回归流程](docs/feedback_regression.md)。

## 验证会话恢复与隔离

1. 用用户 A、会话 S 连续发送两条相关消息，确认第二条能利用第一条的上下文。
2. 重启 API，再用用户 A、会话 S 追问，确认仍能接续；在 PostgreSQL 中确认已建立 checkpoint 表。
3. 用户 B 使用 A 的会话 S 必须返回 404；为 A 另外创建会话 T，确认不会读到 A/S 的短期会话内容。

测试时选择临时对话内容，避免长期记忆自动保存或召回影响对 checkpoint 隔离的判断。离线回归可运行：

```bash
python -m unittest discover -s tests -p 'test_checkpoint_flow.py'
python -m unittest discover -s tests -p 'test_memory_update.py'
python -m unittest discover -s tests -p 'test_recall_routing.py'
python -m unittest discover -s tests -p 'test_stream_recovery.py'
python -m unittest discover -s tests -p 'test_chat_workflow.py'
python -m unittest discover -s tests -p 'test_feedback_api.py'
python -m unittest discover -s tests -p 'test_feedback_loop.py'
python -m unittest discover -s tests -p 'test_chat_storage.py'
python -m unittest discover -s tests -p 'test_chat_persistence.py'
python -m unittest discover -s tests -p 'test_chat_api.py'
```

`test_stream_recovery.py` 使用假模型验证 SSE 生成失败、checkpoint 清理、同会话重试，以及完整回答后记忆更新失败的边界。现有 `test_gemini.py`、`test_search.py` 等脚本可能在导入时访问外部服务，请使用上面的定向命令，避免直接执行全量 `unittest discover` 或 pytest 默认收集。

GitHub Actions 会在每次 push 和 pull request 时自动运行以上十组离线回归，配置位于 `.github/workflows/offline-regression.yml`。这些工作流不连接真实模型、PostgreSQL 或 Milvus。

以下为仓库原有的验证记录，不能代替当前认证和反馈流程的完整端到端验收。

此前已在真实 PostgreSQL 的临时 schema 中执行 checkpoint 初始化，以假聊天模型完成两轮调用，并在关闭、重建连接池后恢复出 4 条 human/AI 消息；临时 schema 已清理。此前 FastAPI 的实际生命周期启动和 `/health` 检查也已通过（HTTP 200、数据库已连接）。另用两个真实数据库会话确认同一 advisory lock 的第二个请求会等待第一个事务提交。完整 `/api/chat`、真实模型与 Milvus 的端到端流程、跨用户隔离及并发负载仍需联调。

## 当前边界

- `/api/chat` 从 JWT `sub` 获取 `user_id`，路径、主题和任务接口加入了所有权检查；双用户端到端验收仍未完成。独立 Graph 示例的记忆工具仍需可信身份绑定；不能仅凭复合线程 ID 断言整个项目已完成访问控制。
- 旧版 `InMemorySaver` 中的历史不会自动迁移到 PostgreSQL；新部署从新的 checkpoint 开始。
- 当前 `/api/chat` 用独立连接池的 PostgreSQL session advisory lock 互斥同一会话；高并发延迟、多进程与进程退出边界仍需在真实负载下验证。checkpoint 写入与 `ChatLog` 提交使用不同连接和事务，因此日志提交失败时，checkpoint 可能已经更新；需要按业务要求补偿或重试。
- `src/agent/graph.py` 是独立的 Graph 学习入口，尚未与 API 的会话持久化流程统一；不要把它的示例行为当作 `/api/chat` 的验收结果。

## 项目文档

- [问题与改进 TODO](docs/TODO.md)：问题状态、验收条件和后续工作。
- [自动召回学习文档](docs/auto_recall.md)：长期记忆读取、向量检索与 Graph 改造。
- [聊天业务图](docs/graph.md) 与 [迁移记录](docs/chat_graph_migration.md)：正式流程的节点、失败边界和验证。
- [JWT 学习与集成指南](docs/jwt_auth.md)：JWT 基础、项目身份边界和分阶段集成方案。
- [学习进度路线](docs/learning_progress_roadmap.md) 与 [Agent 学习路线](docs/agent_learning_roadmap.md)：分阶段学习计划。
- [网关基建与评测路线](docs/gateway_evaluation_roadmap.md)：API、模型与工具边界，以及运行和质量指标。
- [反馈回归流程](docs/feedback_regression.md)：用户负反馈的审核、脱敏和版本对比。
- [文档整理规划](docs/documentation_reorganization_plan.md)：当前文档的合并、拆分、归档及迁移顺序。

主要代码入口为 [API](api/server.py)、[Agent](src/agent/main.py)、[记忆工具](src/agent/tools.py) 和 [独立 Graph 示例](src/agent/graph.py)。
