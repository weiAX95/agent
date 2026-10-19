# 聊天流程接入 LangGraph

> 范围说明：本文记录 Codex 工作树 `/Users/zhangweiwei/.codex/worktrees/8358/agent` 中的实现与离线验证。下面的“完成”仅指该独立工作树；桌面项目 `/Users/zhangweiwei/Desktop/code/agent` 当前仍使用 `main.py` 的串行 `chat()` / `chat_stream()`，尚未合入这套业务 Graph。本文保留为迁移和代码合并的审查记录，不能作为主项目已上线的证据。

## 目标与边界

当前 `/chat` 调用 `main.py` 的 `chat()`：回答前召回长期记忆，调用带 PostgreSQL checkpoint 的 Agent 生成回复，回答后判断并更新 Milvus 记忆。目标是把这些业务阶段放入显式 `StateGraph`，让分支与失败阶段可见，并保证已生成的回复不会因回答后的记忆维护失败而丢失。

实验方案保留 `create_agent` 的工具调用循环和 PostgreSQL 会话 checkpoint。外层 Graph 每次请求使用新的临时状态，不另设 checkpoint；长期记忆仍由 Milvus 保存。主项目现有 `/chat` 通过 POST SSE 返回事件，合入前需重新核对流式生成、取消、错误事件、用量统计和日志提交等行为；原实验只覆盖当时工作树中的调用方式。同一会话的 PostgreSQL advisory lock 仍应覆盖聊天调用和日志提交。

## 修改清单与顺序

1. **建立业务图与状态**：在 `src/agent/main.py` 定义本轮请求状态，将召回、回复、候选判断、冲突决策、写入拆为节点，并用条件边跳过不必要的阶段。模块加载时编译一次业务图；`chat()` 保持现有调用参数，向图传入每轮输入并取回回答。原有 Agent checkpoint 继续负责对话历史。
2. **明确失败边界**：回复生成和召回失败仍使请求失败；回复生成后的记忆判断、冲突决策或写入失败记录完整异常并返回已生成的回答。节点状态标明记忆处理失败；不在业务图上自动重试有副作用的写入。
3. **离线回归验证**：更新 `tests/test_checkpoint_flow.py`、`tests/test_recall_routing.py`、`tests/test_memory_update.py` 以运行真实业务图，覆盖会话隔离、召回、记忆更新及失败边界。运行定向测试和语法检查。
4. **同步项目说明**：更新 `readme.md` 和 `docs/graph.md`，区分正式业务图与独立学习示例。

## 逐步实施记录

### 步骤 0：梳理现状（完成）

- `src/agent/main.py`：`chat()` 串行执行各阶段；`build_agent()` 已为内部 Agent 配置 checkpoint。
- `api/server.py`：`/chat` 在数据库锁内调用 `chat()`；回答生成后若记忆更新抛错，当前请求会失败。
- `src/agent/graph.py`：独立示例，使用 `InMemorySaver`，不是 API 路径。
- `tests/`：现有离线测试使用 AST 单独加载函数，迁移时需调整装载方式，避免依赖真实模型和 Milvus。

### 步骤 1：业务图与状态（完成）

- 修改 `src/agent/main.py`：新增 `ChatState`、`build_chat_workflow()` 与五个节点 `recall → answer → judge → resolve → persist`。`judge` 没有候选记忆时直接结束；`resolve` 只在有候选时运行。`chat()` 每轮传入新的请求状态并返回 `answer`。
- 逻辑依据：把原本串行的业务阶段变成可识别的图节点；回复节点继续调用原有带 PostgreSQL checkpoint 的 `agent.invoke()`，并保持 `make_thread_id(user_id, session_id)`。外层图在模块加载时编译一次，不另存图状态，以免与对话 checkpoint 混用。
- 修改 `tests/chat_graph_loader.py` 及三个离线测试文件的装载方式：只从主模块加载实际业务图函数，避免测试时导入真实模型和 Milvus。

### 步骤 2：失败边界（完成）

- 修改 `src/agent/main.py`：`judge`、`resolve`、`persist` 节点通过 `_safe_memory_node()` 记录完整异常与失败阶段；`answer` 已成功时仍返回回答。`recall` 和 `answer` 异常继续向上抛出。
- 逻辑依据：回答后的长期记忆维护不应抹掉已生成的回复；发生记忆写入失败时不能自动重试，否则可能重复写入。`resolve` 仍在任何写入前完成所有决策校验。
- 修改 `tests/test_memory_update.py`：错误候选或错误冲突决策应保留回答、记录失败，并且不写入记忆。修改 `tests/test_checkpoint_flow.py`：模型生成失败仍应使请求失败。

### 步骤 3：回归验证（完成）

- 修改 `tests/chat_graph_loader.py`：AST 加载业务图的状态和节点，用真实 `StateGraph` 编译；测试中的模型、Milvus 由离线对象替代。Graph 的 `TypedDict` 类型注解需要在装载时立即可解析，因此装载器提供 `Any`、`TypedDict` 等类型，不推迟注解求值。
- 修改 `tests/test_checkpoint_flow.py`：验证每轮只向 Agent 发送当前用户消息、线程 ID 隔离、召回提示不写入 checkpoint，以及召回或回答生成失败向上抛出。
- 修改 `tests/test_recall_routing.py`：原有规则、LLM、向量路由用实际业务图执行。
- 修改 `tests/test_memory_update.py`：覆盖插入、替换、冲突决策校验、记忆写入失败后保留回答。错误候选与错误决策在任何写入前结束。
- 验证：checkpoint 14 项、召回路由 7 项、记忆更新 11 项通过；`compileall` 和 `git diff --check` 通过。离线测试没有访问真实模型、PostgreSQL 或 Milvus。

### 步骤 4：项目说明（完成）

- 修改 `readme.md`：说明 `/chat` 运行正式业务图、内层 Agent 保有会话 checkpoint，并链接本记录。
- 修改 `docs/graph.md`：将原先按单个 `chat()` 函数绘制的详细图改为正式业务图说明，逐节点记录召回、缓存、回复、决策和写入逻辑，并说明失败边界及独立 `graph.py` 示例的关系。旧图容易让回答前召回与回答后写入看起来并行，因此改为顺序节点图。

## 已知边界

- 外层图不持久化，因此不能从记忆写入失败处恢复；失败只记录在服务日志中，客户端仍收到回答。
- 记忆写入可能涉及多条记录，外部 Milvus 操作不是跨记录事务。若写入中途失败，已经成功的记录不会自动回滚；本次没有为它们添加自动重试，以避免重复写入。
- 本次验证是离线回归。真实模型、PostgreSQL、Milvus 的端到端运行仍需在具备服务配置的环境中联调。
