# 聊天流程接入 LangGraph：主分支实施记录

状态：已在主分支的同步 `chat()` 与 POST SSE `chat_stream()` 接入请求级业务图；真实模型、PostgreSQL、Milvus 的完整端到端联调仍待完成。独立 `src/agent/graph.py` 学习示例不在本次合并范围。

## 目标与边界

原流程在 `src/agent/main.py` 中顺序调用回答前召回、带 checkpoint 的 Agent 和回答后记忆更新。实验工作树曾将同步流程拆成图节点；主分支已有 SSE 流、token 用量、断线取消和失败后 checkpoint 清理，因此本次将五阶段图同时用于同步与流式入口，保留这些接口行为。

外层业务图每轮使用临时状态，不保存 checkpoint；内层 Agent 继续使用 PostgreSQL checkpoint 和现有用户/会话复合线程 ID。`/chat` 的 POST SSE 事件格式和同会话 PostgreSQL advisory lock 不变。

## 修改步骤、位置与逻辑

1. **建立五阶段图**：新增 [chat_workflow.py](../src/agent/chat_workflow.py) 的 `ChatState`、`ChatServices` 和 `build_chat_workflow()`，按 `recall → answer → judge → resolve → persist` 建边。`judge` 无候选、`resolve` 失败或流式回答停止时提前结束。使用同步和异步节点实现共用业务流程，异步的阻塞召回及记忆操作通过工作线程执行；图在模块加载时编译一次。
2. **复用既有召回与 Agent**：修改 [main.py](../src/agent/main.py) 的 `chat()` 和 `chat_stream()`，两者分别调用图的 `invoke()` 与 `ainvoke()`。`recall` 继续调用现有 `_prepare_chat()`，保持门控、类别路由、向量兜底、临时记忆缓存及动态系统提示；`answer` 继续调用应用持有的 Agent，不把召回提示写进 checkpoint。
3. **保留流式恢复边界**：图的异步 `answer` 节点继续消费 Agent 的 `messages`/`updates` 流，回传 token 和完整回答。模型失败时调用 `_discard_incomplete_turn()`；取消时调用 `_restore_stopped_turn()`，返回 `stopped` 并跳过记忆处理。`api/server.py` 仍负责 `usage`、`done`、`stopped`、`error` 事件和 `ChatLog` 提交。
4. **拆开记忆维护**：`judge` 先检查 `should_extract_memory()`，再校验候选类别与内容；`resolve` 一次读取相关旧记录并校验整批决策；`persist` 执行新增或定点替换。三个回答后节点发生异常时记录失败阶段并返回已生成的回答，不重放有副作用的写入。此行为相对旧版 SSE 有一处变化：记忆维护失败会产生服务端错误日志，客户端仍收到 `done` 与回答。
5. **离线回归**：新增 [test_chat_workflow.py](../tests/test_chat_workflow.py) 验证同步图、流式 token、记忆失败和取消后的状态恢复；用 [chat_graph_loader.py](../tests/chat_graph_loader.py) 给原有 checkpoint、召回、记忆和 SSE 回归注入真实业务图与离线替身。验证主入口持续走图，同时保留原测试对用户/会话隔离、检索路由及 SSE 失败的覆盖。
6. **同步说明**：更新 [graph.md](graph.md)、项目 README、[TODO](TODO.md) 和学习路线，明确正式业务图与独立学习示例的区别。

## 验证与剩余边界

定向离线测试覆盖同步入口、POST SSE 的模型失败清理、同会话重试、取消恢复、记忆决策与召回路由；语法和文档链接检查在合并前运行。离线替身不证明真实服务、模型和数据库联调成功。

外层图不持久化，不能从记忆写入失败处恢复；Milvus 多条写入也不是原子事务，写到一半失败不会自动回滚。主 API 当前仍信任请求体中的 `user_id`，这次 Graph 合并不解决认证与跨用户授权问题。
