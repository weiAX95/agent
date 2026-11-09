# RAG 评测基线：使用与结果解释

状态：工具与合成资料已提供；真实基线尚未运行。问题来源：[issues.md](issues.md)，实施与后续改进：[todo.md](todo.md)。所有命令从仓库根目录执行。

## 资料与约定

- `evals/rag/dataset.json` 固定 24 题：fact（明确事实）、paraphrase（同义改写）、exact（精确标识）、cross_chunk（多证据）、no_answer（无答案）、conflict（冲突）各 4 题。
- 资料为虚构中文 Markdown 与两页 PDF：手册、设备说明、甲乙两组并列有效规则、跨页运输条款。PDF 已随仓库提供，运行不需要 ReportLab，也不需下载字体。
- 手册包含重复流程、表格条件和长段落末尾的分机号码。跨页题同时要求第一页的隔热箱和第二页的记录仪；冲突题必须保留两组不同规则，不选择一个作为统一规则。
- 问题与原文证据在检索前固定。不得根据当前命中倒改预期证据；修改资料、问题或标注后必须建立新基线。
- 原始运行记录、实际文档绑定、人工评审放在已忽略的 `output/rag/`；不提交真实业务资料。默认不调用聊天 API 或任何生成模型。

## 一、准备隔离环境

使用独立测试 PostgreSQL 数据库和 Milvus 实例，使用相同 BGE 本地模型及生产检索代码；不要将评测资料加入真实共享知识库。

通过项目已有启动流程初始化服务。Milvus 必须已经存在 `agent_memory` 和 `agent_knowledge_v1`，Embedding 目录 `~/models/bge-base-zh-v1.5` 必须存在。评测命令不负责初始化集合或下载模型。

运行命令读取现有 `.env`，也接受环境变量覆盖 `DATABASE_URL`、`MILVUS_URI` 等设置。检查它们确实指向测试环境；工具不会打印连接字符串。现有配置模块还要求 JWT 变量，使用该隔离环境已有 API 配置即可。

评测 PostgreSQL 元数据读取使用只读事务。Milvus 先只读检查必要集合存在，再调用现有检索函数；现有资源入口在集合缺失时可能尝试初始化，因此预检缺失会直接失败。建议测试环境的检索身份仅授予查询权限，并在运行期间停止文档管理操作；不以预检替代数据库权限。

## 二、上传与绑定

1. 使用现有知识库界面或文档上传接口，将 `evals/rag/documents/` 的 **5 个文件**全部上传到隔离环境。不要上传 `dataset.json`。
2. 隔离环境的有效文档目录必须恰好是这 5 份，避免未标注资料影响排序。旧 revision 可以存在于向量库，但不得额外存在有效业务文档。
3. 复制绑定模板：

```sh
mkdir -p output/rag
cp evals/rag/bindings.example.json output/rag/bindings.json
```

4. 根据上传结果填写每个文档的真实 `document_id` 和当前 `revision`。SHA-256 已在模板中给出，不要替换成其他资料的哈希。

现有列表 API 不提供 revision，可在隔离 PostgreSQL 中只读查询：

```sql
SELECT id, revision, filename, sha256
FROM knowledge_documents
WHERE deleted_at IS NULL
ORDER BY filename;
```

绑定结构：

```json
{
  "environment": "isolated-local",
  "documents": {
    "handbook": {
      "document_id": "上传结果中的标准小写 UUID",
      "revision": "数据库当前 revision 的标准小写 UUID",
      "sha256": "模板中已有的文件哈希"
    }
  }
}
```

这里只展示一个条目，实际必须包含模板里的全部 5 个文档键；ID 不能重复。`environment` 是人工填写的环境标签，不是自动隔离保证。

## 三、离线校验

```sh
.venv/bin/python -m tools.rag_eval validate
.venv/bin/python -m tools.rag_eval validate --bindings output/rag/bindings.json
```

`validate` 不连接任何服务、不加载 Embedding；检查文件哈希、案例 ID、类别、证据原文/页码和可选绑定格式。JSON 资料路径必须位于数据集目录内。PDF 只使用已安装的 pypdf 提取文字。

数据契约：`documents` 提供 key/path/sha256；每题提供 case_id/category/query/reference_answer/evidence_groups。每个证据组是可替代原文的列表，命中任一项算该组覆盖；跨片段和冲突题至少两组。每项包含 document/text，可选 page。无答案题必须有空 evidence_groups 和空 reference_answer。

## 四、运行检索

```sh
.venv/bin/python -m tools.rag_eval run \
  --bindings output/rag/bindings.json \
  --out output/rag/baseline.json
```

- 运行前后查询 PostgreSQL，严格核对有效文档 ID、revision、文件哈希及资料范围；发生变化则本次运行无效，不产出有效基线文件。
- 每题调用生产 `search_knowledge_chunks(query, limit=5)`，保留当前阈值。记录结果、分数、片段 ID、正文、页码、耗时及异常，不上传、删除或替换资料。
- 每题异常单独保存并继续执行余题。只要存在失败，命令返回 1，仍输出记录；参数、绑定、资料或版本错误返回 2。成功返回 0。
- 自动生成 `baseline.reviews.json` 人工评审模板；不覆盖已有文件。省略 `--out` 时生成 `output/rag/` 下带 UTC 时间的唯一文件名。
- 结果包括资料/问题集哈希、实际绑定、Git commit 与 dirty 状态、Embedding 路径及配置文件哈希、当前分块参数、检索阈值等。工作区 dirty 时无法仅靠 commit 完全复现，应另行保留相应代码差异。
- 分块参数读取自当前代码，不代表数据库记录了历史索引配置。首次上传必须使用相同代码；现有系统没有持久化旧索引的分块及模型版本。修改分块后比较时，应在隔离环境重新上传并绑定，再比较相同原始资料的运行。
- 耗时包含各题检索整个调用过程；未额外预热，首题可能含 Embedding 冷加载。必须记录该限制，不把小样本 p95 外推为生产容量。

## 五、人工答案评审与报告

在测试聊天中手动提出对应问题，复制实际答案到生成的 reviews 文件。可以分批填写；保留模板顶部的 `run_sha256`，它绑定本次运行结果。

每题字段：

- `answer`：实际答案。没有执行则保留空字符串；不要把 reference_answer 当成实际模型回答。
- `citations_valid`：引用能否对应本轮真实来源。
- `facts_supported`：关键事实是否有证据支持，包括条件、范围和例外。
- `conflict_explained`：冲突题是否分别指出差异；非冲突题保留 null。
- `notes`：不足、来源核查记录、手工聊天的时间/模式，以及与检索运行之间的环境变化。

判断值仅接受 true、false 或 null。空项保持“未评审”，不默认为通过或失败。评审时应保证聊天仍使用同一资料集；普通聊天可能有不同检索路由，在 notes 中注明，不把手工聊天自动视为本次原始检索输出的严格回放。

```sh
.venv/bin/python -m tools.rag_eval report \
  --run output/rag/baseline.json \
  --reviews output/rag/baseline.reviews.json \
  --out output/rag/baseline-report.json
```

也可以省略 `--reviews` 只看自动指标。生成同名 JSON 与 Markdown 报告，包含逐题原始结果和耗时；人工结论仅统计已明确填写的布尔值，未评审项不进分母。

### 自动指标的分母

- **Hit@5**：成功执行的有答案问题中，任一预期证据在前五条结果出现的比例；不是答案正确率。
- **证据覆盖率**：每个成功有答案问题的命中证据组数 / 必需组数，再取题目平均。冲突题只找回一组，即使 Hit@5 为 1，覆盖率仍不足。
- **无答案误召回率**：成功执行的无答案题中返回非空片段的比例；不是“误引用率”，是否误引用由人工判定。
- **失败率**：执行异常题数 / 全部题数。异常不会算成无答案的正确空结果；必须与成功样本数一起阅读，不能靠失败排除来宣称准确率提高。
- **耗时**：报告全部请求（含失败）的原始毫秒值、样本量、p50、p95。分位数使用排序后的线性插值，空分母为 null/不适用。

匹配方式是统一去除空白后的原文包含，并要求文档 ID 一致；有页码标注时还要求页码一致。它不做语义判断，也不会将不同文档中的相同句子当作正确来源。不得用这些自动指标宣称最终答案完全可信。

## 六、同资料版本对比

```sh
.venv/bin/python -m tools.rag_eval compare \
  --baseline output/rag/baseline.json \
  --current output/rag/candidate.json \
  --out output/rag/comparison.json
```

必须是相同问题集及原始资料哈希，案例集合一致；可使用不同实际上传 ID/revision，便于分块更新后重新上传。配置与代码差异保存在 JSON 中。资料或题目变化会拒绝比较。

逐题列出改善、退化、不变、新失败、恢复或持续失败，及耗时差。分类依据为证据覆盖率（有答案）或是否避免无答案误召回（无答案），不代表模型答案改善。比较不合并人工评审；人工答案结果在各运行独立报告中审核。

## 七、另行执行的集成场景

以下不混入静态 24 题指标，不由工具自动修改数据库：

1. **替换**：同文档新 revision 生效后只能返回新事实；旧事实不可作为证据。
2. **删除**：元数据软删除后，即使旧向量清理失败，也不得返回该文档。
3. **部分写入**：索引批次失败或元数据未提交，新 revision 不得对外可见。
4. **并发与漂移**：评测过程中替换、删除或新增有效文档，本次运行应停止有效报告交付。
5. **废弃向量堆积**：单独记录候选扩张、漏检与耗时，评估 1000 条窗口边界。
6. **路由与引用**：手动比较普通聊天与 /knowledge，检查尾部证据、假编号、冲突表述及无答案拒答。

当前只能标记“工具已交付”。真实隔离环境执行及人工审核完成，并保留结果、版本和限制后，才标记“基线已建立”。不设置未经验证的准确率达标门槛。

## 本轮离线验证记录（2026-11-09）

- 新增评测测试 13 项通过；`validate` 通过，确认 5 份资料、24 个问题及固定哈希。
- 已将评测测试加入现有离线 CI，PDF 解析沿用 requirements.txt 中的 pypdf 6.16.2，无新运行依赖。
- 本地运行现有 CI 的 11 个测试组：10 组通过；`test_checkpoint_flow.py` 共 14 项，其中 4 项失败，原因是现有 API 测试夹具缺少 `ChatTurn.model_profile` 和 `available_profiles`。本轮没有修改这些 API 或夹具。
- 未执行真实 PostgreSQL/Milvus 检索，未调用生成模型，也没有上传或更改业务文档。
