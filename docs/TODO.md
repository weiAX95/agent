# 项目问题与后续工作 TODO

更新日期：2026-10-19。本文记录问题、修复状态及验收条件。完成一个条目后，先补充验证结果，再勾选复选框。

原理、当前实现及学习实验见 [自动召回学习文档](auto_recall.md)。本文跟踪工作，该文解释原理。

## 优先级与建议修复顺序

- **P1**：影响数据隔离、记忆正确性或基本启动，优先处理。
- **P2**：影响特定流程、示例运行、可靠性或维护，随后处理。
- **P3**：功能规划与体验优化，基础流程稳定后安排。

建议按以下顺序实施，每一步均使用隔离的测试数据：

1. 依赖、导入和配置：直接依赖已补齐，仍需完成 BUG-08 的干净环境验证，以及 BUG-03、BUG-04；配合 IMP-01，使离线验证不依赖外部服务。
2. 身份和会话隔离：BUG-01 的复合线程 ID 已进入主流程；当前代码已有 JWT 与资源所有权实现，仍需认证回归、Graph 示例工具身份绑定（BUG-07）及真实 PostgreSQL 验收，才能进入多人使用场景。
3. 新增与替换的代码修复已一起完成：BUG-02、BUG-05；下一步做真实 Milvus 联调。
4. Graph 多轮状态与调用示例：BUG-06、BUG-12。
5. 数据导入、PDF 输出和演示：BUG-09、BUG-10、BUG-11、BUG-13。
6. 验证 PostgreSQL checkpoint 的完整 API 重启恢复、advisory lock 并发边界，继续召回效果评估及原有 Web 计划。
7. 按 IMP-04～IMP-07 建立 API 接入、模型调用、运行观测与离线评测基线；按 IMP-08 完成业务 Graph 的真实服务联调。
8. 已接入的用户负反馈按 IMP-07 审核、脱敏后进入回归集；数据一致性与记忆生命周期见 DATA-01、MEM-01，暂不实施。

## 已确认问题

### BUG-01 · P1 · 会话历史未按用户隔离

- [x] 主 API 聊天与调试读取统一使用用户和会话组成的线程 ID。
- [ ] 验收从可信认证上下文确定 `user_id` 及会话归属；当前 `/chat` 已使用 JWT `sub`，但尚无双用户端到端证据。
- [ ] 使用完整 API、两个用户和多个会话完成端到端隔离与重启恢复验证；目前仅验证 PostgreSQL checkpoint 的基础恢复。
- **位置**：[checkpoint.py](../src/agent/checkpoint.py) 的 `make_thread_id()`；[main.py](../src/agent/main.py) 的 `chat()`、`debug_memory()`；[server.py](../api/server.py) 的聊天接口。
- **原现象 / 触发条件**：`chat()` 只用 `session_id` 作为 `thread_id`。两个不同用户使用相同会话 ID 时，会命中同一份聊天历史。长期记忆查询的用户过滤不能隔离短期聊天历史。
- **当前处理**：`make_thread_id(user_id, session_id)` 生成无歧义的复合 ID；主入口聊天和调试读取共用该函数。当前 API 已移除 `ChatRequest.user_id`，从 JWT `sub` 获取聊天身份；独立 Graph 学习入口和记忆工具尚未对齐，完整访问控制仍待验收。
- **后续步骤**：补充 JWT 与双用户隔离回归，验证会话归属，并将可信身份规则推广到独立 Graph 入口和记忆工具。
- **验收条件**：用户 A、B 使用相同 `session_id` 时各自只能看到自己的消息；同一用户同一会话能继续上下文；同一用户不同会话互不混入；调试读取遵守相同规则。

### BUG-02 · P1 · 新增同类记忆会覆盖已有记录

- [x] 让每条新增记忆拥有独立主键，并在已有同类记忆时允许 `insert` 决策。
- [ ] 在真实 Milvus 上验证旧固定主键记录与新 UUID 记录共存。
- **位置**：[tools.py](../src/agent/tools.py) 的 `_insert_memory()`、`save_to_memory()`；[main.py](../src/agent/main.py) 与 [graph.py](../src/agent/graph.py) 的 `insert` 分支。
- **原现象 / 触发条件**：主键只由 `user_id + memory_key` 生成，再执行 `upsert`。同一用户、同一类别下新增“喜欢咖啡”和“喜欢茶”两个独立事实时，第二次会覆盖第一次；“独立主键”的注释与实现不一致。
- **修复步骤**：保留 `memory_key` 作为分类字段，为每条新增事实生成独立 ID；新增使用插入语义，更新使用明确的旧 ID。与 BUG-05 一起修改主入口、Graph 和工具签名，并制定已有固定主键记录的兼容或迁移方案。
- **当前实现**：`_insert_memory()` 用 UUID 和 `insert`；按 `user_id + memory_key` 字段读取全部记录，因此旧固定主键无需迁移即可参与判断；主流程对同类独立事实允许 `insert`。同一条消息中的同类多个候选事实仍受 `judge_memory()`“每 key 最多一项”的限制。
- **离线验证**：`tests/test_memory_update.py` 覆盖同类独立事实共存、重复事实 `none` 零写入、跨用户隔离和定点替换。
- **验收条件**：同类独立事实有不同 ID 且同时存在；不同用户互不影响；重复事实的 `none` 决策不写入；更新不会额外新增记录。

### BUG-03 · P1 · 包导入依赖启动目录

- [ ] 统一包结构、导入方式和启动命令。
- **位置**：[server.py](../api/server.py) 的项目根目录 `sys.path` 补丁；[graph.py](../src/agent/graph.py)、[smith.py](../src/agent/smith.py) 的裸模块导入；[Graph 示例](../src/agent/test/test_graph.py)、[数据查看脚本](../src/agent/test/showData.py) 等。
- **现象 / 触发条件**：API 目前通过项目根目录 `sys.path` 补丁导入 `src.agent.main`；Graph 仍使用 `from model import model` 等裸模块导入。不同入口依赖不同的启动目录或搜索路径，尚未形成统一可安装的包结构。
- **修复步骤**：将项目配置为可安装的包，或统一使用与当前布局匹配的包导入；同步修正内部相对导入、API 和示例，移除不再需要的 `sys.path` 补丁；记录根目录启动命令。
- **验收条件**：干净环境中，从根目录按文档启动 API、导入 Graph 和运行示例不出现 `ModuleNotFoundError`；不需要切换到 `src/agent/`。先配合 IMP-01 避免离线导入触发外部初始化。

### BUG-04 · P1 · 主入口没有主动加载 `.env`

- [ ] 统一加载并校验模型配置。
- **位置**：[main.py](../src/agent/main.py) 的模块级 `init_chat_model()`；[model.py](../src/agent/model.py) 的 `dotenv.load_dotenv()`。
- **现象 / 触发条件**：主入口直接读取 `os.getenv("OPENAI_MODEL")` 等值，其导入链没有加载 `.env`；另一条 Graph 入口才加载了它。只配置 `.env`、没有导出进程环境变量时，主入口可能读到空值，缺少模型名时无法正常聊天。先排除 BUG-03 及外部初始化问题。
- **修复步骤**：建立共同配置入口，在创建模型前加载约定位置的 `.env`；校验必填项、明确环境变量优先级，不打印凭证。统一项目中的 `OPENAI_BASE_URL` / `OPENAI_API_BASE_URL` 命名，按需兼容旧名。
- **边界说明**：命名不统一是维护问题，不能据此断言 SDK 的环境变量回退机制无效。
- **验收条件**：仅配置 `.env` 能得到正确配置；显式环境变量按约定覆盖；缺少必填项时清晰报错；日志不包含 API Key。

### BUG-05 · P1 · 替换逻辑丢弃目标记忆 ID

- [x] 按选中的旧记录 ID 更新记忆，并校验目标归属。
- [ ] 在真实 Milvus 上验证 UUID 与旧固定主键记录的更新。
- **位置**：[graph.py](../src/agent/graph.py) 的 `persist_memory_node()`；[tools.py](../src/agent/tools.py) 的 `replace_memory()`；[main.py](../src/agent/main.py) 的 resolution 结构和替换分支；[旧记录准备脚本](../src/agent/test/test_replace_prepare.py)。
- **原现象 / 触发条件**：Graph 校验了 resolver 返回的 `memory_id`，却未传给替换函数；后者重新计算固定主键。替换准备脚本中的 UUID 记录时，会在另一 ID 上写入新事实，旧记录仍保留。主入口的 resolution 结构也没有明确选择目标 ID。
- **修复步骤**：替换函数显式接收 `memory_id`，确认目标存在且属于当前用户后再更新。Graph 传递已校验目标；主入口结构化决策也携带并校验目标 ID。与 BUG-02 一起明确新增、替换、无操作语义，不能将目标不存在静默当成新增。
- **当前实现**：主流程批量决策携带 `memory_id`，写入前核对类别、数量和目标 ID；Graph 将选中的 ID 传给 `replace_memory()`；工具再次查询目标并校验用户与类别，然后复用原 ID 更新文本和向量。
- **离线验证**：`tests/test_memory_update.py` 覆盖无效、跨用户、跨类别 ID 拒绝更新，同类别其他记录保持不变，以及 Graph 按 UUID 旧 ID 更新。真实 Milvus 联调仍待完成。
- **验收条件**：更新 UUID 旧记录后 ID 和记录总数不变，内容与向量更新；同类别其他记录不变；不存在、跨用户或不允许的目标被拒绝；`none` 不写入。

### BUG-06 · P2 · Graph 消息字段没有合并规则

- [ ] 为多轮消息配置正确的 reducer。
- **位置**：[schemas.py](../src/agent/schemas.py) 的 `AgentState.messages`；[graph.py](../src/agent/graph.py) 的 `agent_node()`。
- **现象 / 触发条件**：`messages` 是普通列表。启用 checkpoint 后，第二次只提交新用户消息时，该列表会替换原列表，导致多轮上下文丢失。
- **修复步骤**：使用 `MessagesState`，或为 `messages` 配置 `Annotated[..., add_messages]`；检查节点返回完整历史还是本轮增量，以及消息 ID 是否稳定，防止修复覆盖后又引入重复追加。
- **验收条件**：同一线程两轮调用只提交新消息也能保留历史；消息不重复；不同线程不共享消息；工具调用及对应响应顺序完整。

### BUG-07 · P1 · Graph 工具调用没有绑定可信 `user_id`

- [ ] 从运行上下文为记忆工具注入当前用户身份。
- **位置**：[graph.py](../src/agent/graph.py) 的 `agent_node()`；[smith.py](../src/agent/smith.py) 的 agent；[tools.py](../src/agent/tools.py) 的 `search_memory(user_id, query)`。
- **现象 / 触发条件**：外层 state 有 `user_id`，但内部 agent 只收到 `messages`；记忆工具要求模型自行填写 `user_id`。身份没有被程序绑定，模型可能无法构造正确查询或填入错误身份。主入口把 ID 写进 system message 也只是提示，不能代替工具层校验。
- **修复步骤**：用运行上下文、工具包装器或依赖注入绑定服务端确定的身份；从模型参数中移除 `user_id`，或执行前强制校验/覆盖。自动召回、主动搜索、保存和替换使用同一可信身份来源，与 BUG-01 一致。
- **验收条件**：内部工具使用的身份始终等于外层可信身份；模型或用户文本填写其他 ID 不能改变查询范围；覆盖自动召回与主动工具搜索两条路径。

### BUG-08 · P1 · 主流程直接依赖未完整声明

- [x] 明确声明当前直接使用的 `langchain`、`langgraph` 和 PostgreSQL checkpoint 依赖。
- [ ] 在干净环境中验证完整依赖安装、导入和离线测试。
- [ ] 解决依赖冲突：`gradio==6.8.0` 要求 `aiofiles<25`，而当前 `unstructured-client==0.46.2` 要求 `aiofiles>=25.1.0`；本地 `pip check` 因此仍失败。评估是否拆分服务/解析环境或选择兼容版本，再重建依赖锁定文件。
- **位置**：[requirements.txt](../requirements.txt)；[main.py](../src/agent/main.py)、[graph.py](../src/agent/graph.py)、[smith.py](../src/agent/smith.py) 的 `langchain` / `langgraph` 导入。
- **原现象 / 触发条件**：清单曾包含多个 LangChain 子包，却未直接声明使用的 `langchain` 和 `langgraph`。当前环境已安装的包可能掩盖问题；不能依赖偶然的传递依赖保证直接使用的模块可用。
- **当前处理与后续**：清单现已列出 `langchain`、`langgraph`、`langgraph-checkpoint-postgres` 及 psycopg 依赖。仍需在干净环境验证版本兼容，并区分服务运行、解析和开发测试依赖；Python 版本、嵌入模型及 Milvus 等外部条件见项目说明。
- **验收条件**：新建环境仅按文档安装后，依赖检查通过、直接使用模块可导入、离线测试通过。模型文件和外部服务另行准备，不混同于包安装成功。

### BUG-09 · P2 · 中文切块可能超出 VARCHAR 字节上限

- [ ] 按 UTF-8 字节容量验证导入文本。
- **位置**：[create_db.py](../scripts/milvus/create_db.py) 的 `text.max_length=1024`；[insert_db.py](../scripts/milvus/insert_db.py) 的 `chunk_size=400`。
- **现象 / 触发条件**：切块按字符，容量按字节。400 个常见汉字占 1200 字节，会超过 1024 字节；emoji 也需要考虑。[Milvus VARCHAR 官方说明](https://milvus.io/docs/string.md)。
- **修复步骤**：写入前校验 `len(text.encode("utf-8"))`，按容量重新切块，或调整字段容量并规划迁移；处理重叠、标点和 emoji 边界。不要直接用当前建表脚本迁移数据：它会删除同名集合，应先改为明确的迁移步骤。
- **验收条件**：纯中文、中英混合、emoji 和边界长度文本均满足容量；超长输入有明确处理，不无提示截断；验证使用临时集合，不删除现有数据。

### BUG-10 · P2 · 同名 PDF 的结果相互覆盖

- [ ] 为不同输入生成独立输出路径。
- **位置**：[pdf_parser.py](../src/parsers/pdf_parser.py) 的结果下载与解压部分。
- **现象 / 触发条件**：ZIP 和解压目录只使用 `pdf_path.stem`。不同目录的同名 PDF 共用输出路径，已有目录会被 `shutil.rmtree()` 删除；同文件重跑也会直接替换旧结果。
- **修复步骤**：使用输入路径/内容摘要或任务 ID 构造唯一位置；明确重跑时复用、版本化或显式覆盖的策略；先在临时目录成功解压，再切换最终结果。
- **验收条件**：不同目录中的 `report.pdf` 输出互不覆盖；失败重试保留有效旧结果；同文件重跑遵循文档约定；使用临时目录和模拟 ZIP 离线验证。

### BUG-11 · P2 · Gemini 示例没有保存响应对象

- [ ] 修复 `http_post()` 的响应变量。
- **位置**：[test_gemini.py](../tests/test_gemini.py) 的 `http_post()`。
- **现象 / 触发条件**：requests 分支没有把 `requests.post(...)` 返回值赋给 `response`，随后调用其方法，会访问未赋值的局部变量。
- **修复步骤**：接收 response，统一使用传入 URL 和明确的 timeout，保留状态码校验与 JSON 解析；用 mock 覆盖 requests 和 urllib 回退分支。
- **验收条件**：模拟成功响应返回 JSON，HTTP 错误明确上报；测试不访问 Gemini、不依赖真实 Key。

### BUG-12 · P2 · Graph 示例缺少 checkpoint 配置

- [ ] 给 Graph 调用提供 `configurable.thread_id`。
- **位置**：[test_graph.py](../src/agent/test/test_graph.py) 的 `graph.invoke()`；[graph.py](../src/agent/graph.py) 的图编译。
- **现象 / 触发条件**：Graph 使用 `InMemorySaver`，示例却没有传 checkpoint 配置。state 里的 `session_id` 不会自动变成 config 中的 `thread_id`；修复导入后仍会遇到 checkpoint 配置错误。
- **修复步骤**：显式传 config，使用 BUG-01 的统一身份/会话标识；说明业务 state 与执行 config 的区别；将顶层执行整理为明确测试或演示入口。
- **验收条件**：使用假模型和存储能执行一轮并按 thread ID 取回状态；两轮验证满足 BUG-06；只导入示例不触发外部请求。

### BUG-13 · P2 · 演示源码浏览器仍使用旧路径

- [ ] 更新文件映射、运行命令和 HTTP 错误处理。
- **位置**：[index.html](../web/index.html) 的 `files`、`loadFile()`；实际文件位于 [demos](../demos/)、[tests](../tests/)、[projects/crypto_bot](../projects/crypto_bot/) 等目录。
- **现象 / 触发条件**：`fetch(file.name)` 请求页面同目录文件，但多个文件已经移动；运行命令仍用旧路径。未检查 `response.ok` 时，404 页面可能被当成源码显示。
- **修复步骤**：分开设置显示名、请求路径与命令；确定静态服务根路径，只提供允许浏览的演示文件；修正或移除失效映射，检查 HTTP 状态。不为修复路径而直接公开整个仓库与配置文件。
- **验收条件**：通过约定 HTTP 服务打开页面，各项展示真实对应内容或明确错误；从根目录复制运行命令有效；404 不作为源码显示。

## 工程改进与召回迭代

以下是后续改进，不等同于已证明发生了线上故障。

### IMP-01 · P2 · 区分离线测试、集成测试与演示

- [ ] 消除测试收集和模块导入时的外部副作用。
- **位置**：[tools.py](../src/agent/tools.py)、[main.py](../src/agent/main.py)、[test_api.py](../tests/test_api.py)、[test_search.py](../tests/test_search.py)、[Graph 测试目录](../src/agent/test/)。
- **现状**：工具模块导入时加载 embedding、连接 Milvus 并可能建集合；一些 `test_*.py` 在顶层访问服务、调用模型或写数据库。默认测试收集可能触发真实请求或因服务缺失失败。
- **实施步骤**：依赖构造移到工厂或应用生命周期；注入模型、embedding 和存储；手动演示使用显式入口；离线测试使用 fake/mock，集成测试单独标记并使用临时资源，写库准备脚本不参与默认收集。
- **验收条件**：没有真实 Key、模型和数据库时，默认收集和离线测试通过；集成测试须明确选择；假客户端可以验证过滤、新增、替换和阈值行为。

### IMP-02 · P2 · 明确持久化与并发边界

- [x] 主 API 复用现有 PostgreSQL，将 Agent checkpoint 从 `InMemorySaver` 迁到 PostgreSQL。
- [x] 在真实 PostgreSQL 临时 schema 中用假聊天模型验证连接池重建后的两轮基础恢复。
- [x] 启动建表及 `PostgresSaver.setup()` 使用事务级 advisory lock 协调多 worker 初始化。
- [x] `/chat` 用 PostgreSQL 事务级 advisory lock 串行化同一用户/会话的请求，锁持有到 `ChatLog` 提交。
- [ ] 使用完整 API、真实模型与 Milvus 验证重启、不同用户同名会话和多进程恢复。
- [ ] 验证同一会话并发、跨 worker 顺序、锁等待超时与高负载延迟，并处理 checkpoint 与 `ChatLog` 不同事务的失败一致性。
- **位置**：[checkpoint.py](../src/agent/checkpoint.py) 的 `checkpoint_dsn()`、`checkpoint_lock_id()`；[main.py](../src/agent/main.py) 的 `build_agent()`；[server.py](../api/server.py) 的生命周期与 `/chat` 锁；独立 [graph.py](../src/agent/graph.py) 仍使用 `InMemorySaver`。
- **当前实现**：API 使用现有 `DATABASE_URL`，将 SQLAlchemy 的 asyncpg URL 转成 checkpoint 驱动可用的 PostgreSQL DSN，在应用生命周期中初始化、持有和关闭连接池。启动时的 SQLAlchemy 建表与 `PostgresSaver.setup()` 在固定的事务级 advisory lock 下执行，避免多个 worker 的 checkpoint 迁移竞态。`/chat` 根据用户/会话稳定生成锁键，先在 SQLAlchemy 事务里获取 `pg_advisory_xact_lock`，再调用 Agent，直到日志提交才释放；PostgreSQL 可协调不同 worker 的同一会话请求。`ChatLog` 保留为问答日志，不承担 checkpoint 恢复；旧内存 checkpoint 不自动迁移。
- **验证记录**：2026-10-02，在真实 PostgreSQL 的临时 schema 执行 `PostgresSaver.setup()`；假聊天模型两轮调用中间关闭并重建连接池，恢复后包含 4 条 human/AI 消息，临时 schema 已删除。FastAPI 实际生命周期启动和 `/health` 返回 HTTP 200；两个真实数据库会话验证同一 advisory lock 会等待前一事务提交。该验证不覆盖完整 `/chat`、真实模型、Milvus、跨用户隔离或并发负载。
- **后续步骤**：在完整 API 下验证同线程并发顺序及等待上限、多进程部署和高负载行为。checkpoint 使用独立连接池，与日志写入不是单个原子事务；若日志提交失败而 checkpoint 已成功，需要定义重试或补偿策略。如需让独立 Graph 入口持久化，另行设计其状态模型与连接生命周期。
- **验收条件**：声明支持的部署方式下，重启可恢复；并发消息没有重复或混乱；用户隔离回归验证通过。

### IMP-03 · P2 · 评价召回效果并定义故障降级

- [x] 回答前先执行门控，并区分 LLM 否决与“需要召回但无法确定类别”；显式回忆问句放行，泛知识问句不由类别规则直查。
- [ ] 用固定样本评价召回规则和失败行为。
- **位置**：[main.py](../src/agent/main.py) 的 `should_recall_memory()`、`chat()`；[tools.py](../src/agent/tools.py) 的 `_recall_memories()`；[学习文档](auto_recall.md)。
- **现状**：主入口业务图的 `recall` 节点先运行门控：显式回忆短语（如“记得我”“我之前说过”）直接放行；其他问题采用“个人信号 AND 记忆主题”关键词组合。门控为 `False` 时跳过回答前的类别规则、LLM 和向量查询。门控通过后，类别规则只对少数完整的本人事实问句（如“我现在做什么工作？”）直接给出类别；“我想了解职业方向有哪些？”之类泛问交给 LLM。规则无类别时，LLM 返回 `should_recall` 与 `memory_keys`；LLM 否决则不检索，确认需要且给出类别则精确读取，确认需要但类别为空才做向量兜底，传 `limit=5`、`min_score=0.4`。`_recall_memories()` 单独调用的默认阈值是 `0.45`。这些参数不能当作已验证的最优值；独立 `graph.py` 学习示例仍未对齐正式入口。回答后的记忆写入判断仍可能读取同类旧记录，不受回答前门控跳过的约束。
- **离线验证**：[test_recall_routing.py](../tests/test_recall_routing.py) 的 7 个测试覆盖门控跳过、规则精确查询、LLM 否决、LLM 给定类别精确查询、LLM 确认但无类别时一次向量兜底、无主题显式回忆问句，以及泛职业问题遵守 LLM 否决；2026-10-03 定向运行全部通过。此验证使用假模型与假存储，不证明真实模型分类和 Milvus 召回质量。
- **实施步骤**：准备应召回、不应召回、信息更新、无结果和近义表达样本，并纳入当前会漏触发的“这个岗位适合我吗？”；记录触发率、命中质量、时延与误召回，按实际度量解释分数并集中管理阈值；定义 embedding/Milvus 失败时的降级；评估正式业务图的 `recall` 节点。
- **验收条件**：样本能重复比较规则与参数；无结果、低分和服务失败行为可预测；跨用户样本不命中；日志不暴露不必要的个人信息；Graph 接入状态与真实召回效果分别记录。

### IMP-04 · P1 · API 接入网关与可信身份

- **当前进度**：当前代码已加入 JWT 注册/登录、`/chat` 身份读取和路径/主题/任务所有权检查；真实双用户隔离、前端接入、限流与网关部署尚未验收。
- [ ] 明确入口部署形态：FastAPI 内部中间件负责什么，反向代理或托管 API 网关负责什么；记录 TLS、可信转发头、健康检查和故障路径，不按产品名先选型。
- [ ] 与 BUG-01、BUG-07 共用可信身份来源：认证后确定 `user_id`，校验会话归属，工具调用不能由模型参数改写身份。
- [ ] 定义请求大小、并发数、速率限制和取消处理；若使用多个 worker，限流状态须共享或由统一入口执行。
- [ ] 统一 401、403、413、429、5xx 的响应和日志字段；生成请求 ID 并贯穿入口、Graph、模型与工具。
- **位置**：[server.py](../api/server.py)、认证/中间件模块、[checkpoint.py](../src/agent/checkpoint.py)、[tools.py](../src/agent/tools.py)。
- **验收条件**：伪造请求体 `user_id` 不能读取他人会话或记忆；跨 worker 压测仍满足限流；超大请求与限流请求有明确响应；一次失败可凭请求 ID 串起日志；未认证请求不能进入聊天流程。

### IMP-05 · P2 · 模型调用网关

- [ ] 将模型供应商、模型名、地址、密钥、超时及重试策略收敛到同一配置与调用边界，消除模块导入时创建真实模型的副作用（关联 BUG-04、IMP-01）。
- [ ] 区分 429、超时、上游 5xx、配置错误和模型输出错误；只对可安全重试的调用设置有界退避，不重放工具写入或长期记忆写入。
- [ ] 记录模型、供应商、输入/输出 token、耗时及带价格版本的费用估算；设置请求级预算或上限，并明确超限行为。
- [ ] 若要提供模型切换或故障转移，先验证工具调用、结构化输出和回答质量兼容；保留开关与回退路径。
- **位置**：[main.py](../src/agent/main.py) 的模型构造、统一配置模块、模型调用适配层。
- **验收条件**：使用假模型稳定复现超时、429、5xx、预算超限和切换；日志不含密钥；各场景只发生允许的重试次数；同一评测集能比较切换前后的质量、延迟和费用。

### IMP-06 · P2 · 运行观测与故障定位

- [ ] 检查当前 POST SSE `/chat` 的 `Chat stream failed` 异常链，关联请求 ID、失败阶段与服务端异常栈；客户端继续收到可解析的 `error` 事件。
- [ ] 为 API、现有聊天阶段、模型、Milvus、PostgreSQL 和工具调用记录 trace/span、成功状态、错误类别及耗时；请求 ID 与 trace ID 可相互关联。
- [ ] 建立请求量、错误率、端到端 p50/p95/p99、各阶段延迟、模型超时/429、工具失败、记忆写入失败、token 和费用看板；对当前 SSE 接口补充首 token 延迟和中断率。
- [ ] 只用低基数标签（路由模板、阶段、状态类、模型等）聚合指标；用户 ID、会话 ID、请求 ID、原始提示词和异常文本不作指标标签，日志及 trace 按需脱敏。
- [ ] 基于首轮真实基线设定服务目标和告警阈值，记录采样、留存及异常追踪策略。
- **位置**：[server.py](../api/server.py)、[main.py](../src/agent/main.py)、工具与存储调用边界。
- **验收条件**：一次人为注入的模型、检索或写入故障能在 trace 中定位；看板区分网关拒绝、模型故障和记忆维护故障；无敏感信息进入指标标签。

### IMP-07 · P2 · 离线评测集与指标报告

- [x] 建立真实聊天负反馈的收集入口、人工审核与脱敏闸门、隔离 API 运行命令，以及按样本 ID 对比基线/当前结果的工具。`tests/test_feedback_api.py` 和 `tests/test_feedback_loop.py` 验证身份边界、重复反馈、批准条件及回退检测；操作见 [反馈回归流程](feedback_regression.md)。
- [ ] 建立版本化样本集，至少覆盖无需召回、需单/多类别召回、无结果、旧事实更新、冲突事实、跨用户隔离、工具失败和带指令的恶意记忆；为每条样本保存预期路由、相关记忆、写入动作与答案依据。
- [ ] 计算路由 precision/recall/F1、类别命中率；检索 Hit@K、Recall@K、Precision@K、空结果率与跨用户命中数；记忆 `insert`/`replace`/`none` 准确率和重复写入率。
- [ ] 评估最终回答的任务完成率、有依据回答比例、事实错误率与正确承认未知的比例；自动评分需抽样人工复核，不把 LLM Judge 当作唯一真值。
- [ ] 将质量指标与 p95 延迟、token、费用、错误率放在同一份报告，按场景分组，并记录数据集、代码、模型、提示词、阈值和价格版本。
- [ ] 固定一条离线运行命令及基线结果；每次调整模型、路由、阈值或聊天流程时生成前后对比，首轮基线后再设质量门槛。
- **当前范围**：反馈导出、审核提升、确定性打分和两版结果对比已有实现；真实模型观察结果与语义评测基线尚未在此仓库归档，不能把示例或单元测试当成质量验收。
- **位置**：新增离线评测数据与运行器、`tests/`、[自动召回文档](auto_recall.md)；与 IMP-03 共用召回样本。
- **验收条件**：同一配置重复运行可得到可解释的波动范围；报告能指出提升和回退的具体样本；跨用户泄漏为 0；真实服务指标与离线质量分数明确分开。
- **指标口径**：见 [网关基建与评测路线](gateway_evaluation_roadmap.md)。

### IMP-08 · P2 · 业务 Graph 联调与验收

- [x] 将请求级 Graph 合入主项目，对齐 `chat_stream()`、取消处理、`usage` / `done` / `error` 事件和 PostgreSQL advisory lock，并更新 [Graph 说明](graph.md)、README 和离线回归。
- [x] 独立的 `/Users/zhangweiwei/Desktop/code/web/my-app` 已实现登录、POST SSE、流式回复、重试与停止；2026-10-19 在该目录运行 `npm run build` 通过，并确认本地后端 `8001/health` 返回 200。此项证明前端实现和后端可达，不代替完整聊天联调。
- [ ] 完成 API、真实模型、PostgreSQL 与 Milvus 的端到端联调，再确认部署环境中的流式和记忆行为。
- **验收条件**：现有 SSE 事件和取消恢复行为保持正确；Graph 的召回、回答、记忆写入边界可测；主项目代码与文档一致。

### DATA-01 · P2 · checkpoint、ChatLog 与记忆写入的一致性

- [ ] 定义 checkpoint 已成功而 `ChatLog` 提交失败时的补偿、重放或对账规则，并验证客户端重试不会把一轮对话计为两轮。
- [ ] 对长期记忆的新增和替换设计稳定的幂等标识，覆盖中断、超时和重复执行后的记录数量与目标 ID。
- **位置**：[server.py](../api/server.py)、[chat_workflow.py](../src/agent/chat_workflow.py)、[tools.py](../src/agent/tools.py)。本项按用户要求先登记为待办。

### MEM-01 · P2 · 长期记忆生命周期

- [ ] 提供按可信用户身份查看、纠正和删除个人记忆的能力；删除后同时验证 Milvus 检索、缓存和后续回答不再使用该事实。
- [ ] 定义过期事实、相互冲突事实和误提取事实的处理规则，以及保留期与审计范围。
- **位置**：[tools.py](../src/agent/tools.py)、记忆 API 与前端。此项按用户要求先登记为待办。

## 原有六项计划：保留并更新状态

下面保留旧 TODO 的六项原意。已有代码仍有相关缺陷或尚未完成验收，因此主复选框继续保持待办。

### PLAN-01 · P2 · 整理 API 目录与入口

- [ ] 完成目录和导入约定的验收。
- **原计划**：“创建 src/api/__init__.py 和 src/api/server.py 目录结构”。
- **当前状态：已有实现，位置调整**。现在实际位于根目录的 [api/__init__.py](../api/__init__.py)、[api/server.py](../api/server.py)，不是 `src/api/`。
- **下一步 / 验收**：记录选定布局并完成 BUG-03，不为旧路径再创建一套重复 API；启动命令能从根目录执行。

### PLAN-02 · P2 · 封装可调用 Agent Loop

- [ ] 完成聊天函数的调用与依赖隔离验收。
- **原计划**：“从 tools.py 导入工具，把 main.py 的 Agent Loop 封装为可调用函数”。
- **当前状态：已有实现**。[main.py](../src/agent/main.py) 的 `chat(message, session_id, user_id, agent)` 接收由 `build_agent(checkpointer)` 构造的 Agent；API 在应用生命周期内创建并注入它。
- **下一步 / 验收**：结合 BUG-01、BUG-04、IMP-01，验证 API 和离线调用复用同一函数，且配置、身份和外部依赖行为明确。

### PLAN-03 · P2 · 提供 POST /chat

- [ ] 完成聊天接口的端到端验收。
- **原计划**：“添加 POST /chat 接口，接收 message + session_id，返回 answer”。
- **当前状态：已有实现**。[server.py](../api/server.py) 以 SSE 返回 `token`、`usage`、`done`、`stopped` 或 `error` 事件；`usage` 仅展示供应商提供的真实 token 统计。当前接口使用 Bearer 认证，并从 JWT 获取身份，不再接受请求体 `user_id`。
- **下一步 / 验收**：完成独立 `my-app` 的真实浏览器联调和双用户 API 隔离验收；验证有效请求、缺失字段、流中异常和断开连接。

### PLAN-04 · P2 · 暂存会话并迁移 checkpointer

- [x] 主 API 使用 PostgreSQL checkpoint 并绑定复合线程 ID。
- [ ] 完成完整 API 的真实数据库恢复、并发和认证验收；底层 PostgreSQL checkpoint 基础恢复已验证。
- **原计划**：“用内存 dict 按 session_id 暂存对话历史（临时方案，后续换 LangGraph checkpointer）”。
- **当前状态：主 API 已改造，Graph 示例未改造**。API 使用 PostgreSQL checkpointer；独立 `graph.py` 仍用 `InMemorySaver`，且存在 BUG-06 的消息合并问题。
- **下一步 / 验收**：无需重新加入内存 dict；完成 BUG-01 的可信身份、BUG-06、IMP-02 的真实 PostgreSQL 和并发验证，说明两条入口的部署边界。

### PLAN-05 · P3 · 提供静态聊天页面

- [x] 在独立 `my-app` 目录实现 React 聊天页面，接入登录、POST SSE、用量显示、重试与停止；2026-10-19 构建通过。
- [ ] 完成真实浏览器两轮聊天验收，并确定正式部署时的页面托管与路由。
- **原计划**：“挂载 web/ 目录为静态文件，支持直接访问 chat.html”。
- **当前状态**：页面位于 `/Users/zhangweiwei/Desktop/code/web/my-app`，不在本 Git 仓库内。开发时 Vite 将 `/api` 代理至本地 FastAPI；[server.py](../api/server.py) 未挂载静态目录，仓库中的 `web/index.html` 仍是源码浏览器。
- **下一步 / 验收**：在真实浏览器完成两轮对话、错误与反馈提交流程；记录前端源码的版本管理和正式部署位置。

### PLAN-06 · P3 · 配置本地开发 CORS

- [ ] 根据实际跨域需求加入明确配置。
- **原计划**：“添加 CORS 中间件，允许本地跨域请求”。
- **当前状态：后续规划**。[server.py](../api/server.py) 没有 CORS 中间件。
- **下一步 / 验收**：列出实际本地 origin，按需允许方法、请求头和凭据；允许的页面可请求 `/chat`，其他 origin 不获跨域许可；若改成同源页面，记录是否还需要 CORS。

### DEPLOY-01 · P3 · 生产环境 JWT 密钥管理与轮换

- [ ] 正式部署前，将 JWT 签名密钥迁移到部署平台的 Secret Manager / KMS / Vault，通过运行时身份注入；生产密钥与开发、测试密钥分开，禁止写入 Git、镜像或日志。
- [ ] 根据部署拓扑确定签名方式：单一受控 API 可使用高熵 HS256 密钥；多个服务验证或使用外部身份提供方时，优先使用 OIDC/JWKS 或非对称签名，验证端只持有公钥。
- [ ] 实现密钥轮换：支持 `kid` 选择可信密钥，在过渡期接受新旧密钥；定义旧密钥退役、令牌失效及泄漏后的紧急撤销流程。
- [ ] 设置短期 access token，并评估 refresh token 的轮换与撤销存储；验证 `iss`、`aud`、`exp`、固定算法及令牌签名。
- **当前状态**：当前代码已集成自签发 JWT；生产密钥托管、轮换和撤销尚未实现。学习与接口改造步骤见 [JWT 指南](jwt_auth.md)。开发阶段可用未提交到仓库的本地配置验证流程；本条延后到生产部署准备阶段处理，不把本地 `.env` 当作生产密钥管理方案。
- **验收条件**：生产密钥不出现在源码、镜像和日志中；密钥可审计并可轮换；滚动发布期间合法令牌仍可验证；旧密钥退役及疑似泄漏处置流程经过演练。

## 检查范围与验证限制

- 本文依据源码与现有测试更新状态；语法和离线测试通过不代表真实服务启动、跨用户隔离与数据持久化都已通过。
- 2026-10-02 的会话改造以 [checkpoint.py](../src/agent/checkpoint.py)、[main.py](../src/agent/main.py) 和 [server.py](../api/server.py) 的实际代码与测试结果为准。本文记录了设计及尚未完成的完整 `/chat`、真实模型和 Milvus 联调，不将离线验证视为端到端验收。
- 一部分缺陷可从源码与数据流确认；Milvus 实际行为、SDK/包版本兼容、真实召回效果、API 部署和浏览器整体验收，仍需在隔离环境中验证。
- 后续修复记录应包含：关联编号、代码变更、离线验证结果、集成验证是否执行及其范围。不要仅凭文档描述勾选完成。

## 2026-10-19 文档同步记录

- [README 中文](../README.md) 与 [英文](../README.en.md)：统一认证配置与 `/chat` Bearer 请求示例，避免旧版请求体 `user_id` 误导新代码使用者。
- [Graph 迁移记录](chat_graph_migration.md)、[自动召回](auto_recall.md)、[网关与评测路线](gateway_evaluation_roadmap.md)：将“Graph 待合入”改为“正式图已合入”，并更新回答后记忆失败的边界；独立学习 `graph.py` 的待办保持独立。
- [JWT 指南](jwt_auth.md)、[JWT 实施草案](jwt_implementation_guide.md) 和本 TODO：记录本地已编写的后端认证与资源归属代码，保留前端、双用户联调和生产密钥管理为待验收；草案中的旧行号只供设计回顾。
- [学习进度路线](learning_progress_roadmap.md) 与 [Agent 学习路线](agent_learning_roadmap.md)：把早期状态明确标成历史快照，当前学习重点改为真实服务联调、运行观测与质量评测。
- 校验：此前五组 Graph/会话/召回/记忆/SSE 离线回归 38 项通过；本次扩展为七组共 52 项通过，`git diff --check` 与所改文档的相对链接检查通过。独立 `my-app` 构建通过；本地反馈 API 对隔离记录的 401、404、201、409 已验收并清理数据。真实模型聊天、Milvus 与浏览器两轮端到端验收仍待记录。
