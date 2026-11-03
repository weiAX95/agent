# 共享知识库 RAG 完善计划

状态：代码已接入，真实运行与效果待用户验收。个人长期记忆 `agent_memory` 不在本轮改动范围。

## 已确定的产品边界

- 所有登录用户共享知识库，并可上传、替换和删除文档。
- 首版接收 TXT、Markdown、DOCX、PDF；扫描版 PDF 若提取不到文字，明确提示无法入库。
- 保留原有 `demo_collection` 演示数据；正式知识库使用独立集合。
- 功能体验与真实 Milvus 验收由用户自行安排，本轮只做静态核对。

## 实施顺序

1. **文档管理与入库**：限制上传大小及解析量，提取页码，切块、向量化，保存文档元数据，建立非破坏性的正式 Milvus 集合。提供列表、上传、替换、删除接口。
2. **检索与来源**：主 Agent 仍通过 research 子 Agent 检索；知识库命中含文档、页码、片段标识、摘要和分数。低相关或空结果明确返回，不让模型把无关片段当证据。来源由工具结果提取，不采信模型自己编造的链接。
3. **前端体验**：登录后可管理共享文档；执行过程和回答旁可查看来自网页或知识文档的来源，刷新后仍可读取已持久化来源。
4. **评估准备**：提供固定问题与预期文档的人工记录模板，便于用户后续比较召回与答案质量。

## 接口约定

- `GET /api/knowledge/documents`：列出共享文档。
- `POST /api/knowledge/documents`：multipart `file` 上传。
- `PUT /api/knowledge/documents/{id}`：multipart `file` 替换。
- `DELETE /api/knowledge/documents/{id}`：删除文档及检索片段。
- 研究事件 `sources` 保留原有网页 URL 字符串，同时允许知识文档来源对象：`kind=knowledge`、`document_id`、`chunk_id`、`title`、`page`、`excerpt`。旧历史网页事件仍可读取。

## 完成边界

代码、接口和文档交付后，仍需用户在真实 PostgreSQL、Milvus、模型及浏览器中验证。没有这些结果时，不把检索准确率或实际运行状态写为“已通过”。
