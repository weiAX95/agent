# 当前 Agent 学习路线

更新日期：2026-10-29。这份短路线只维护**当前阶段和下一步**。详细原理保留在 [2026-09-12 Agent 学习稿](history/agent_learning_roadmap.md) 与 [2026-09-30 执行路线](history/learning_progress_roadmap.md)，其中旧状态是历史快照。

| 阶段 | 当前代码证据 | 下一步 |
| --- | --- | --- |
| 模型、LangChain、RAG 基础 | `demos/`、`src/agent/tools.py` | 保留为学习示例，避免与正式 API 入口混用。 |
| 会话与记忆 | PostgreSQL checkpoint、Milvus、`/api/chat` | 持续梳理身份、失败恢复和跨会话记忆的真实边界。 |
| 主子 Agent 与过程可见 | `research` 子 Agent、`report_progress`、`chat_run_events` | 比较检索收益、耗时和成本；完善总执行预算与失败语义。 |
| 产品任务链 | 已有 `LearningPath`、`Topic`、`Task` | 按 [产品执行清单](../tasks/todo.md) 实现目标澄清、计划草稿、确认保存和每日推进。 |
| 评测与交付 | 反馈接口和离线回归工具 | 建立真实样本基线、运行观测和重复部署流程。 |

本学习路线不代替 [产品路线](../tasks/plan.md)，也不把代码存在或离线回归等同于用户验收。
