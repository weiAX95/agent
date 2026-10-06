# LLM Agent

[English](README.en.md) · [项目进度](docs/TODO.md)

基于 FastAPI、LangChain / LangGraph 的聊天 Agent 实验项目，探索流式响应、PostgreSQL 会话恢复与 Milvus 长期记忆。

> **状态：实验阶段。** 已有离线回归测试和部分集成验证记录；真实模型、Milvus 与完整聊天接口的端到端联调尚未完成。当前不作为可直接对外部署的成品。

## 项目关注的问题

| 问题 | 当前实现 |
| --- | --- |
| 服务重启后的上下文恢复 | PostgreSQL checkpoint |
| 不同用户与会话的历史隔离 | 稳定的复合会话标识 |
| 同一会话的并发请求 | PostgreSQL 事务级 advisory lock |
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

启动 API：

```bash
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000 --reload
```

首次启动会初始化 API 使用的 SQLAlchemy 表和 LangGraph checkpoint 表；初始化过程由 PostgreSQL advisory lock 串行化，避免多个 API worker 同时执行迁移。检查数据库连接，再发送聊天请求：

```bash
curl http://127.0.0.1:8000/health

curl -N -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"user_id":"demo-user","session_id":"demo-session","message":"你好"}'
```

## 工程细节

`/chat` 使用 SSE 返回 `token`、`usage`、`done` 或 `error` 事件。`usage` 是模型供应商经 LangChain 返回的真实 token 统计；供应商未返回时标记不可用，不按字符数伪造估算。前端以 `fetch` 发送 POST JSON 并读取响应流；浏览器原生 `EventSource` 不支持这里使用的 POST 请求体。收到 `done` 时，`data.answer` 是完整回复。[main.py](src/agent/main.py) 用 `make_thread_id(user_id, session_id)` 生成稳定且无歧义的 checkpoint 线程 ID；同一用户、同一会话会继续之前的短期历史，不同用户复用相同 `session_id` 也不会意外共用线程。API 启动时创建 PostgreSQL 连接池和 Agent，关闭时释放连接。每轮只提交当前用户消息；召回内容通过动态系统提示传入，不累积到持久化消息历史中。Agent 回复和工具消息仍由 checkpoint 保存。`ChatLog` 只记录问答，不承担 checkpoint 恢复状态的工作。

`/chat` 在调用 Agent 前为当前用户/会话获取 PostgreSQL 事务级 advisory lock，并持有到 `ChatLog` 提交；同一会话的并发请求由数据库串行化，跨 API worker 也共享此约束。不同会话使用不同锁键，可并行处理。

## 验证会话恢复与隔离

1. 用用户 A、会话 S 连续发送两条相关消息，确认第二条能利用第一条的上下文。
2. 重启 API，再用用户 A、会话 S 追问，确认仍能接续；在 PostgreSQL 中确认已建立 checkpoint 表。
3. 用用户 B、会话 S 发送相同追问，以及用用户 A、会话 T 发送相同追问，确认都不会读到 A/S 的短期会话内容。

测试时选择临时对话内容，避免长期记忆自动保存或召回影响对 checkpoint 隔离的判断。离线回归可运行：

```bash
python -m unittest discover -s tests -p 'test_checkpoint_flow.py'
python -m unittest discover -s tests -p 'test_memory_update.py'
python -m unittest discover -s tests -p 'test_recall_routing.py'
python -m unittest discover -s tests -p 'test_stream_recovery.py'
```

`test_stream_recovery.py` 使用假模型验证 SSE 生成失败、checkpoint 清理、同会话重试，以及完整回答后记忆更新失败的边界。现有 `test_gemini.py`、`test_search.py` 等脚本可能在导入时访问外部服务，请使用上面的定向命令，避免直接执行全量 `unittest discover` 或 pytest 默认收集。

GitHub Actions 会在每次 push 和 pull request 时自动运行以上四组离线回归，配置位于 `.github/workflows/offline-regression.yml`。这些工作流不连接真实模型、PostgreSQL 或 Milvus。

以下为仓库原有的验证记录，本次文档整理未重新执行：\n\n目前已在真实 PostgreSQL 的临时 schema 中执行 checkpoint 初始化，以假聊天模型完成两轮调用，并在关闭、重建连接池后恢复出 4 条 human/AI 消息；临时 schema 已清理。FastAPI 的实际生命周期启动和 `/health` 检查也已通过（HTTP 200、数据库已连接）。另用两个真实数据库会话确认同一 advisory lock 的第二个请求会等待第一个事务提交。完整 `/chat`、真实模型与 Milvus 的端到端流程、跨用户隔离及并发负载仍需联调。

## 当前边界

- 请求体中的 `user_id` 尚未通过身份认证。复合会话 ID 能防止不同用户意外共用同一 checkpoint，但调用方仍可伪造他人的 `user_id`；对外提供服务前，需要从可信认证上下文获取身份，并校验会话归属。
- 旧版 `InMemorySaver` 中的历史不会自动迁移到 PostgreSQL；新部署从新的 checkpoint 开始。
- 当前 `/chat` 用 PostgreSQL advisory lock 串行化同一会话；锁等待超时、高并发延迟和多进程效果仍需在真实负载下验证。checkpoint 写入与 `ChatLog` 提交使用不同连接和事务，因此日志提交失败时，checkpoint 可能已经更新；需要按业务要求补偿或重试。
- `src/agent/graph.py` 是独立的 Graph 学习入口，尚未与 API 的会话持久化流程统一；不要把它的示例行为当作 `/chat` 的验收结果。

## 项目文档

- [问题与改进 TODO](docs/TODO.md)：问题状态、验收条件和后续工作。
- [自动召回学习文档](docs/auto_recall.md)：长期记忆读取、向量检索与 Graph 改造。
- [学习进度路线](docs/learning_progress_roadmap.md) 与 [Agent 学习路线](docs/agent_learning_roadmap.md)：分阶段学习计划。

主要代码入口为 [API](api/server.py)、[Agent](src/agent/main.py)、[记忆工具](src/agent/tools.py) 和 [独立 Graph 示例](src/agent/graph.py)。
