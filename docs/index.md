# 项目文档导航

更新日期：2026-10-29。以下“当前”表示与仓库现有代码对照后的说明，不表示真实环境已验收。前端位于独立目录 `../web/my-app`，不在本仓库的 Git 提交中。

## 当前实现

| 主题 | 从这里读起 |
| --- | --- |
| 产品版本与下一步 | [产品路线](../tasks/plan.md)、[执行清单](../tasks/todo.md) |
| 聊天、主子 Agent、执行过程 | [正式聊天业务图](graph.md) |
| 会话、消息和事件表 | [当前会话数据结构](session_current.md) |
| 长期记忆 | [当前记忆机制](memory_current.md) |
| 身份与授权 | [JWT 与项目身份边界](jwt_auth.md) |
| 反馈与评测 | [反馈回归](feedback_regression.md)、[网关与评测路线](gateway_evaluation_roadmap.md) |
| 前端聊天 | [前端当前接入说明](../../web/my-app/docs/chat_current.md) |

## 记录与学习

- [按日期查看改动](change_history.md)、[工程问题台账](TODO.md)、[文档整理计划](documentation_reorganization_plan.md)。
- 实施记录：[聊天 Graph 迁移](history/chat_graph_migration.md)、[会话存储交付](history/session_storage_delivery.md)、[首版会话表设计](chat_session_schema.md)、[执行过程目标](../tasks/execution_timeline_goal.md)、[会话存储目标](../tasks/session_storage_goal.md)。
- 用户负责的验收场景：[跨会话记忆](cross_session_memory_acceptance.md)。
- 学习材料：[当前学习路线](learning_current.md)、[自动召回原理与实验](auto_recall.md)、[2026-09-12 Agent 学习稿](history/agent_learning_roadmap.md)、[旧版详细学习路线](history/learning_progress_roadmap.md)、[JWT 原实施草案](history/jwt_implementation_guide.md)。
- 独立实验文件 `codex_deepseek_fallback.md` 尚未纳入本次文档迁移提交。`test.pdf` 是被 Git 忽略的本地 PDF 解析示例输入，由 `src/parsers/pdf_parser.py` 引用，不作为项目说明入口。

阅读当前代码时，以“当前实现”区的文档为入口。旧实施记录保留当时的约束、步骤和结果；遇到与当前代码不一致的描述，以当前实现和代码为准。
