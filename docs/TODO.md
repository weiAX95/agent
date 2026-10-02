# 项目问题与后续工作 TODO

更新日期：2026-09-30。本文记录问题、修复状态及验收条件。完成一个条目后，先补充验证结果，再勾选复选框。

原理、当前实现及学习实验见 [自动召回学习文档](auto_recall.md)。本文跟踪工作，该文解释原理。

## 优先级与建议修复顺序

- **P1**：影响数据隔离、记忆正确性或基本启动，优先处理。
- **P2**：影响特定流程、示例运行、可靠性或维护，随后处理。
- **P3**：功能规划与体验优化，基础流程稳定后安排。

建议按以下顺序实施，每一步均使用隔离的测试数据：

1. 依赖、导入和配置：BUG-08 → BUG-03 → BUG-04；配合 IMP-01，使离线验证不依赖外部服务。
2. 身份和会话隔离：BUG-01、BUG-07，进入多人使用场景前完成。
3. 新增与替换的代码修复已一起完成：BUG-02、BUG-05；下一步做真实 Milvus 联调。
4. Graph 多轮状态与调用示例：BUG-06、BUG-12。
5. 数据导入、PDF 输出和演示：BUG-09、BUG-10、BUG-11、BUG-13。
6. 推进持久化、召回效果评估及原有 Web 计划。

## 已确认问题

### BUG-01 · P1 · 会话历史未按用户隔离

- [ ] 修复会话身份与 checkpoint 的绑定。
- **位置**：[main.py](../src/agent/main.py) 的 `chat()`、`debug_memory()`；[server.py](../api/server.py) 的聊天接口。
- **现象 / 触发条件**：`chat()` 只用 `session_id` 作为 `thread_id`。两个不同用户使用相同会话 ID 时，会命中同一份聊天历史。长期记忆查询的用户过滤不能隔离短期聊天历史。
- **修复步骤**：统一生成由用户身份与会话 ID 组成的无歧义 `thread_id`；聊天、调试读取和 Graph 均使用它。API 用于多人场景时，用户身份应来自可信认证上下文，并校验会话归属，而不是只相信请求体里的 ID。
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
- **位置**：[server.py](../api/server.py) 的 `from agent.main import chat`；[graph.py](../src/agent/graph.py)、[smith.py](../src/agent/smith.py) 的裸模块导入；[Graph 示例](../src/agent/test/test_graph.py)、[数据查看脚本](../src/agent/test/showData.py) 等。
- **现象 / 触发条件**：代码位于 `src/agent/`，API 却导入 `agent.main`；Graph 使用 `from model import model` 等导入。从项目根目录按包方式启动时，默认模块搜索路径不能找到这些模块。手工修改 `PYTHONPATH` 或切换目录会让不同入口行为不一致。
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

- [ ] 补齐可重建运行环境的依赖清单。
- **位置**：[requirements.txt](../requirements.txt)；[main.py](../src/agent/main.py)、[graph.py](../src/agent/graph.py)、[smith.py](../src/agent/smith.py) 的 `langchain` / `langgraph` 导入。
- **现象 / 触发条件**：清单包含多个 LangChain 子包，却未直接声明使用的 `langchain` 和 `langgraph`。当前环境已安装的包可能掩盖问题；不能依赖偶然的传递依赖保证直接使用的模块可用。
- **修复步骤**：核对运行时直接依赖，补充兼容版本约束；区分服务运行、解析和开发测试依赖；记录 Python 版本、嵌入模型及 Milvus 等外部条件。
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

- [ ] 为会话历史制定持久化和并发方案。
- **位置**：[main.py](../src/agent/main.py)、[graph.py](../src/agent/graph.py) 的 `InMemorySaver`。
- **现状**：checkpoint 在进程内存，重启不保留，多进程不自然共享；适合学习和单进程演示，持久化需求需另行设计。
- **实施步骤**：按部署方式选择持久化 checkpointer，沿用统一用户/会话标识；定义生命周期、同会话并发串行或冲突处理，以及迁移兼容策略。
- **验收条件**：声明支持的部署方式下，重启可恢复；并发消息没有重复或混乱；用户隔离回归验证通过。

### IMP-03 · P2 · 评价召回效果并定义故障降级

- [ ] 用固定样本评价召回规则和失败行为。
- **位置**：[main.py](../src/agent/main.py) 的 `should_recall_memory()`、`chat()`；[tools.py](../src/agent/tools.py) 的 `_recall_memories()`；[学习文档](auto_recall.md)。
- **现状**：主入口采用“个人信号 AND 记忆主题”的关键词门控，传 `limit=5`、`min_score=0.4`；工具默认阈值是 `0.45`。这些是当前参数，不能当作已验证的最优值；Graph 尚未实现与主入口等价的回答前自动注入。
- **实施步骤**：准备应召回、不应召回、信息更新、无结果和近义表达样本；记录触发率、命中质量、时延与误召回，按实际度量解释分数并集中管理阈值；定义 embedding/Milvus 失败时的降级；评估回答前召回节点。
- **验收条件**：样本能重复比较规则与参数；无结果、低分和服务失败行为可预测；跨用户样本不命中；日志不暴露不必要的个人信息；是否接入 Graph 有明确记录。

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
- **当前状态：已有实现**。[main.py](../src/agent/main.py) 已有 `chat(message, session_id, user_id)` 并导入工具。
- **下一步 / 验收**：结合 BUG-01、BUG-04、IMP-01，验证 API 和离线调用复用同一函数，且配置、身份和外部依赖行为明确。

### PLAN-03 · P2 · 提供 POST /chat

- [ ] 完成聊天接口的端到端验收。
- **原计划**：“添加 POST /chat 接口，接收 message + session_id，返回 answer”。
- **当前状态：已有实现**。[server.py](../api/server.py) 已声明接口，实际请求还要求 `user_id`，返回 `answer`。
- **下一步 / 验收**：修复启动和身份隔离，明确身份来源；验证有效请求、缺失字段及异常响应；客户端示例与请求结构一致。

### PLAN-04 · P2 · 暂存会话并迁移 checkpointer

- [ ] 完成会话隔离和持久化规划。
- **原计划**：“用内存 dict 按 session_id 暂存对话历史（临时方案，后续换 LangGraph checkpointer）”。
- **当前状态：部分完成，已有替代实现**。当前已用 `InMemorySaver`，但仍为内存存储；只使用 `session_id` 的主入口有 BUG-01。
- **下一步 / 验收**：无需重新加入内存 dict；完成 BUG-01、BUG-06、IMP-02，说明恢复和部署边界。

### PLAN-05 · P3 · 提供静态聊天页面

- [ ] 实现聊天页面并挂载静态文件。
- **原计划**：“挂载 web/ 目录为静态文件，支持直接访问 chat.html”。
- **当前状态：后续规划**。[server.py](../api/server.py) 未挂载静态目录，仓库没有 `web/chat.html`；现有 `web/index.html` 是源码浏览器。
- **下一步 / 验收**：实现页面、明确静态路由、对齐 `/chat` 结构；浏览器能完成两轮对话及展示错误；身份和会话传递符合 BUG-01、BUG-07。

### PLAN-06 · P3 · 配置本地开发 CORS

- [ ] 根据实际跨域需求加入明确配置。
- **原计划**：“添加 CORS 中间件，允许本地跨域请求”。
- **当前状态：后续规划**。[server.py](../api/server.py) 没有 CORS 中间件。
- **下一步 / 验收**：列出实际本地 origin，按需允许方法、请求头和凭据；允许的页面可请求 `/chat`，其他 origin 不获跨域许可；若改成同源页面，记录是否还需要 CORS。

## 检查范围与验证限制

- 依据上一轮审查结论，并在编写本文时重新核对上述源码和原 TODO。上一轮记录检查了 34 个 Python 文件，语法检查通过，关键问题做过离线复现；语法通过不代表启动、隔离和数据正确性通过。
- 本次仅整理文档，没有修复业务代码，没有调用真实模型或写入数据库，没有读取或展示密钥。
- 一部分缺陷可从源码与数据流确认；Milvus 实际行为、SDK/包版本兼容、真实召回效果、API 部署和浏览器整体验收，仍需在隔离环境中验证。
- 后续修复记录应包含：关联编号、代码变更、离线验证结果、集成验证是否执行及其范围。不要仅凭文档描述勾选完成。
