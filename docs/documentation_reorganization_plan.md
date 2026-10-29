# 文档合并与拆分实施计划

更新日期：2026-10-29。代码基线：`ca7ccbc`（主子 Agent 与执行过程）、`3841ed8`（流式恢复断言）。本文是**文档改造计划**，尚未按计划迁移正文。功能代码已提交；前端 `../web/my-app` 不是 Git 仓库，不在这两笔提交中。

## 整理原则

1. 每个主题只有一份“当前代码如何工作”的文档；实施记录、旧方案和学习练习保留日期，不能与当前说明并列作为操作指引。
2. 优先合并内容和修正状态，其次才移动文件。大量文档已有相对链接，避免为目录整齐而一次改动全部路径。
3. 产品路线、执行清单、问题台账、改动历史分别回答不同问题，不拼成一篇巨型文档。
4. 后端和前端各维护自己项目的文档；跨项目只用链接关联，不复制 API 说明的两套正文。

## 目标导航

| 读者的问题 | 唯一当前入口 | 其他文档的角色 |
| --- | --- | --- |
| 现在要做什么？ | `tasks/plan.md`（版本路线）、`tasks/todo.md`（执行项） | `docs/TODO.md` 仅作为工程问题台账，计划改名 `docs/known_issues.md`。 |
| 之前哪天改了什么？ | `docs/change_history.md` | 提交号与带日期实施记录作为证据。 |
| 正式聊天/主子 Agent 如何运行？ | `docs/graph.md` | `docs/chat_graph_migration.md`、`tasks/execution_timeline_goal.md` 是实施记录，不重复维护当前流程。 |
| 会话和消息怎样存？ | `docs/chat_session_schema.md` | `docs/session_storage_delivery.md`、`tasks/session_storage_goal.md` 是交付记录；当前表结构只在 schema 文档维护。 |
| 记忆怎样读写、跨会话生效？ | 拟新建 `docs/memory_current.md` | `docs/auto_recall.md` 留学习原理与独立 Graph 练习；`docs/cross_session_memory_acceptance.md` 保留验收场景。 |
| JWT 身份当前怎样接？ | `docs/jwt_auth.md` | `docs/jwt_implementation_guide.md` 是历史实施草案，不再提供当前逐行操作步骤。 |
| 怎样处理反馈和网关？ | `docs/feedback_regression.md`、`docs/gateway_evaluation_roadmap.md` | 两者用途独立；实施状态只更新执行清单。 |
| 怎样学习 Agent？ | 拟将 `docs/learning_progress_roadmap.md` 缩为唯一当前学习路线 | `docs/agent_learning_roadmap.md` 保留为 2026-09-12 历史学习稿；旧快照不标成当前进度。 |
| 如何使用独立的前端聊天？ | 拟新建 `../web/my-app/docs/chat_current.md` | 前端原接入教程与执行记录保留为历史资料。 |

## 分批实施

### 第 1 批：纠正入口与状态，不搬文件

- [ ] 新建 `docs/index.md`，按“当前规范、执行记录、学习资料、实验”列出所有文档和最后核对日期；README 指向该索引。
- [ ] 修正 README 中“前端尚未适配”等过期说法；将主子 Agent、执行过程和新迁移的当前状态写清，同时标注前端不在本仓库提交中。
- [ ] `docs/TODO.md` 顶部说明它是工程问题台账，具体产品任务以 `tasks/todo.md` 为准；之后再决定是否改名。
- [ ] 给 `docs/jwt_implementation_guide.md`、两份学习路线的历史段落添加“历史资料/当前入口”提示，不在这一批删正文。

### 第 2 批：合并正式聊天与会话说明

- [ ] 以 `docs/graph.md` 为正式架构入口，补齐 `ca7ccbc` 的事件表、SSE、历史读取和失败状态；从 `docs/chat_graph_migration.md` 只提取仍适用的设计结论，保留迁移记录本身。
- [ ] 以 `docs/chat_session_schema.md` 为当前数据库契约，更新 10 月 26—29 日已实施状态；将 `docs/session_storage_delivery.md` 和 `tasks/session_storage_goal.md` 标为完成记录，旧状态加历史标注。
- [ ] 前端生成 `../web/my-app/docs/chat_current.md`：集中当前请求地址、会话恢复、历史按需加载、执行过程事件。旧 `chat-session-integration.md`、`chat-integration-progress.md` 留作带日期的接入记录；`session-restore.md`、`research-agent-integration.md`、`execution-timeline-ui.md` 中仍有效的内容并入当前指南后留简短入口。

### 第 3 批：拆分超长文档

- [ ] `docs/auto_recall.md`（约 615 行）：正式 Milvus 记忆读写、用户隔离和排障提炼为 `docs/memory_current.md`；原理实验和独立 Graph 练习保留在原文并明确不是 `/api/chat` 的入口。
- [ ] `docs/jwt_implementation_guide.md`（约 1005 行）：仅保留为历史实施草案；将仍有效的身份来源、令牌和权限边界归入 `docs/jwt_auth.md`，不把大段旧代码复制过去。
- [ ] `docs/learning_progress_roadmap.md`（约 853 行）与 `docs/agent_learning_roadmap.md`（约 317 行）：合并当前阶段、代码证据和下一步，删除重复课程目录；历史快照留在原文并从索引标为历史。不把两篇全文拼接。

### 第 4 批：清理重复与收尾

- [ ] `docs/TODO.md` 如改名为 `docs/known_issues.md`，先更新所有入链和 README，再在原路径保留导航页；产品执行状态只在 `tasks/todo.md` 勾选。
- [ ] `docs/feedback_regression.md`、`docs/gateway_evaluation_roadmap.md`、`docs/cross_session_memory_acceptance.md` 保留独立，不强行合并；只补到索引和当前架构文档的交叉链接。
- [ ] `docs/codex_deepseek_fallback.md` 标记为独立实验；核定 `docs/test.pdf` 的用途和来源后再决定是否索引或移出，不直接删除。
- [ ] 核对 README、后端和前端文档的相对链接；确认旧路径能找到新入口、历史日期仍可追溯，之后才删除真正重复的正文。

每批独立提交文档，提交说明写明“当前入口”与被标为历史的文档。用户负责真实功能测试；文档整理不触碰运行代码，也不把未验收功能写成已验收。
