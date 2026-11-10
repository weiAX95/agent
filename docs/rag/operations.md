# RAG 配置、迁移与后台索引

2026-11-10。实现与实测记录见 [verification.md](verification.md)，工作清单见 [todo.md](todo.md)。

## 配置与默认行为

| 配置 | 默认值 | 行为 |
| --- | --- | --- |
| `KNOWLEDGE_CHUNKING_VERSION` | `2` | 按章节切分、保留标题路径；长表格分段重复表头。`1` 可复现原始分块。仍以 800 字符/120 重叠为基础，结构标题另占不超过 200 字符。 |
| `KNOWLEDGE_RETRIEVAL_MODE` | `vector` | `hybrid` 增加 SQL 关键词召回、RRF 融合、标识符优先、去重与每文档最多 3 个结果；不是额外训练的重排模型。 |
| `KNOWLEDGE_EXPAND_NEIGHBORS` | `0` | `1` 在有效 revision 内扩展相邻片段；整体 4000 字符预算，单片段不超过 Milvus 的 4096 UTF-8 字节，保留页码范围。 |
| `KNOWLEDGE_MIN_SCORE` | `0.40` | 向量 COSINE 阈值。关键词命中不伪装成 COSINE 分数，使用 `score_kind=lexical`、`score=null`。 |
| `KNOWLEDGE_AUTO_ROUTE` | `1` | “查知识库”“根据…文档”“我上传的文档”等明确意图直接检索；普通闲聊继续由主 Agent 判断是否 research。`/knowledge` 始终可用。 |
| `KNOWLEDGE_VERIFY_CLAIMS` | `0` | `1` 用当前模型额外检查答案是否有原文支持。检查失败或服务异常返回保守说明；模型判断不能代替人工审核。开启会增加调用、耗时，普通聊天也会缓冲至完成；默认不增加持续调用费用。 |
| `KNOWLEDGE_MANAGEMENT_MODE` | `shared` | 所有登录用户可共享管理。`admin` 时仅允许名单内用户上传、替换、删除、重试或重新索引。 |
| `KNOWLEDGE_ADMIN_IDS` | 空 | 逗号分隔的标准小写用户 UUID，仅在 admin 模式生效。不信任客户端传入的 role，也不授予 JWT 新角色。 |
| `KNOWLEDGE_ARCHIVE_DIR` | `output/knowledge/` | 原文件不可变归档；目录和文件按私有权限创建，文件名来自 revision UUID，绝不使用上传路径。多进程/API/worker 必须挂载同一持久化目录。 |
| `KNOWLEDGE_OCR` | `0` | `1` 对无文字 PDF 页调用本机 Tesseract；需预装程序和语言包。不会联网、下载模型或使用收费解析接口。 |
| `KNOWLEDGE_OCR_LANG` | `chi_sim+eng` | OCR 语言；缺少程序/语言包返回可解释失败。 |
| `KNOWLEDGE_PDF_LAYOUT` | `0` | `1` 启用 pypdf 布局提取；复杂版式效果需用实际资料核对。 |
| `MILVUS_DB_NAME` | `default` | 正式服务默认不变；隔离工具使用唯一测试 database。 |

OCR 最多 20 页，单页渲染最多 2000 万像素、单次执行 30 秒。原有 PDF 100 页、文件 10 MB、正文 20 万字符、300 个块上限继续生效。OCR 默认关闭时，纯扫描件仍明确拒绝入库。

`KNOWLEDGE_PG_SCHEMA` 默认 public，仅供隔离工具选择同步知识库存储 schema；不要单独设置该变量把正式 API 的异步元数据连接与 worker 连接指向不同 schema。

## 索引、归档与历史资料迁移

新增的 `knowledge_index_jobs`、`knowledge_chunk_records`、`knowledge_audit_events` 是独立表；随现有 API 启动的 `Base.metadata.create_all()` 创建，不修改现有 Milvus 集合 schema，也不删除旧文档。正式服务需正常重启才能加载代码和创建新表。此次只在隔离 schema 执行过建表，没有对正式数据库执行迁移或索引重建。

上传和替换先归档原文件，再提交持久任务；完成向量写入后，在同一个 PostgreSQL 事务内切换文档 revision、写入 SQL 分块目录并完成任务。检索仍以 PostgreSQL 的有效 revision 为准。向量 ID 由 revision + chunk_index 确定，以 upsert 支持进程崩溃后的幂等恢复。首次解析结果另以私有、不可变的 parsed manifest 固定，后续重试复用它，避免 OCR 重跑改变分块数量；manifest 核对原文件哈希、索引配置及内容摘要。

旧文档继续参与向量检索。要启用结构化分块或混合检索的 SQL 索引：

1. 有完整归档的新文档：调用 reindex，创建新 revision，旧版本在新版本成功发布前持续可用。
2. 历史文档无原文件归档：reindex 返回 409，需重新上传来源文件。不能从残缺片段伪造原文件。
3. 新策略不覆盖历史 revision；旧向量清理失败不会重新进入检索，后台对账随后清理。

归档保留原文件及任务配置。删除文档会阻止后续检索，但不会自动擦除归档和过去聊天里的内容；备份、归档保留周期与磁盘配额由部署者管理。不要把 `output/` 的 Git 忽略规则误认为持久化备份。

## 接口兼容与后台操作

原列表、上传、替换、删除接口路径不变。默认同步上传成功仍为 201，替换成功为 200；元数据增加 `revision`、`sha256`，便于绑定评测。

| 接口 | 新行为 |
| --- | --- |
| `POST /api/knowledge/documents?background=true&request_id=<UUID>` | 只校验上传边界并归档、入队，202 返回 job ID 和真实状态；解析/Embedding/发布由 worker 执行。 |
| `PUT /api/knowledge/documents/{id}?background=true&request_id=<UUID>` | 后台替换；发布时核对最初 base_revision，冲突则 409/任务 conflict，不能覆盖其他人的新版本。 |
| `GET /api/knowledge/documents/jobs/{job_id}` | 已登录用户可查询 queued/running/succeeded/failed/conflict、次数与版本配置；不返回原文和内部异常详情。 |
| `POST /api/knowledge/documents/jobs/{job_id}/retry` | 管理者显式重试 failed；queued/running/已完成或达到上限时拒绝。 |
| `POST /api/knowledge/documents/{id}/reindex?request_id=<UUID>` | 从完整归档重新提交新 revision，返回 202。 |
| `GET /api/knowledge/documents/{id}/audit?limit=50` | 有界读取该文档的操作记录，最多 100 条。 |

客户端应为同一上传/替换重试复用相同 request_id。相同 ID 对应不同文件、操作者、操作类型或目标会拒绝；未提供 ID 视为新任务。已完成任务对应的文档若随后被替换/删除，返回 409 而不冒充当前请求生成的新结果。

后台索引需要显式运行 worker；不运行 worker 时后台任务保持 queued，不会伪报完成：

```sh
.venv/bin/python -m tools.knowledge_worker work
.venv/bin/python -m tools.knowledge_worker once --job-id <UUID>
```

领取任务使用 PostgreSQL `FOR UPDATE SKIP LOCKED`。租约 180 秒，每 30 秒续租，最多 3 次执行；过期任务可被接管，旧 worker 在发布前核对所有权、状态、有效期。达到上限的过期任务转为 failed。失败不自动无限重试；需要显式 retry 或重新提交。

原文件、解析代码哈希、分块配置、OCR 配置与本地 Embedding 权重/配置内容指纹一起记录。worker 配置或解析代码发生变化时旧任务失败并提示重新提交新 revision，不能悄悄按另一套配置发布。配置相同的进程重启可恢复同一 revision。

## 清理与运行指标

```sh
# 默认只预览，不删除
.venv/bin/python -m tools.knowledge_worker reconcile
# 清理有任务/SQL目录记录的无效修订
.venv/bin/python -m tools.knowledge_worker reconcile --apply
# 显式盘点旧向量中没有任务记录的历史孤立修订，再清理
.venv/bin/python -m tools.knowledge_worker reconcile --scan-vectors --apply
```

清理持有任务/文档权威行锁，跳过当前有效 revision 和 queued/running 修订。worker 长期运行时每 5 分钟对已知修订做一次清理；全库旧向量扫描需操作员显式选择，避免默认对大库反复全扫描。

检索同步连接复用有界池（池容量 4、最多额外 2，等待 5 秒，语句超时 10 秒），应用关闭时释放。关键词 token 使用 JSONB GIN 索引。查询日志/评测原始记录含候选窗口、处理数量、过期过滤、metadata 查询次数与耗时、最终数量、失败标记；计数是各窗口累计处理次数，并非唯一向量数。

候选窗口最多 1000 条。若全被无效版本占满且没有有效结果，明确报告检索不完整，不作为无答案空结果。已有少量有效结果但达到窗口上限时保留结果并记录 `candidate_cap_reached`。这是一项保护与清理机制，不是无限规模性能承诺。

Python INFO 日志需在部署日志配置中启用 `src.agent.knowledge_store`、`src.agent.chat_workflow`；评测工具会直接把 retrieval_trace 写入每题原始记录，不依赖控制台 INFO 配置。

## 答案与流式一致性

显式/自动知识库模式先检索，缓冲生成内容，检查 [K编号] 是否属于本轮来源，之后才发送正文；无结果/不可用时返回保守说明。正文仍支持 Markdown，研究工具内部 JSON 与用户回答格式无关。

开启事实核验时，普通 research 链路也会在拥有内部原文证据时进行支持性核验；网页仅有 URL 的情况不能宣称得到内部文档支持。来源预算优先保留内部知识库证据，避免早先的网页 URL 挤掉全部原文。

校验失败时修改同一 AIMessage ID 的 checkpoint 后才发出保守正文，使 SSE、最终答案和历史一致。若检查期间停止，未审核的草稿不会作为已展示正文保存。关闭事实核验时仍检查知识库引用编号，但不宣称语义已验证。模型核验也可能误判，不能替代固定样本与人工审核。
