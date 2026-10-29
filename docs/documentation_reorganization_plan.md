# 文档合并与拆分实施计划

更新日期：2026-10-29。代码基线：`ca7ccbc`（主子 Agent 与执行过程）、`3841ed8`（流式恢复断言）。本文兼作迁移记录。前端 `../web/my-app` 不是 Git 仓库，不在这两笔提交中。

## 整理原则

1. 每个主题只有一份“当前代码如何工作”的文档；实施记录、旧方案和学习练习保留日期，不能与当前说明并列作为操作指引。
2. 优先合并内容和修正状态，其次才移动文件。大量文档已有相对链接，避免为目录整齐而一次改动全部路径。
3. 产品路线、执行清单、问题台账、改动历史分别回答不同问题，不拼成一篇巨型文档。
4. 后端和前端各维护自己项目的文档；跨项目只用链接关联，不复制 API 说明的两套正文。

## 目标导航

| 读者的问题 | 唯一当前入口 | 其他文档的角色 |
| --- | --- | --- |
| 现在要做什么？ | `tasks/plan.md`（版本路线）、`tasks/todo.md`（执行项） | `docs/TODO.md` 仅作为工程问题台账，保留原路径以免打断旧链接。 |
| 之前哪天改了什么？ | `docs/change_history.md` | 提交号与带日期实施记录作为证据。 |
| 正式聊天/主子 Agent 如何运行？ | `docs/graph.md` | `docs/history/chat_graph_migration.md`、`tasks/execution_timeline_goal.md` 是实施记录，不重复维护当前流程。 |
| 会话和消息怎样存？ | `docs/session_current.md` | `docs/chat_session_schema.md` 是首版设计；`docs/history/session_storage_delivery.md`、`tasks/session_storage_goal.md` 是交付记录。 |
| 记忆怎样读写、跨会话生效？ | `docs/memory_current.md` | `docs/auto_recall.md` 留学习原理与独立 Graph 练习；`docs/cross_session_memory_acceptance.md` 保留验收场景。 |
| JWT 身份当前怎样接？ | `docs/jwt_auth.md` | `docs/history/jwt_implementation_guide.md` 是历史实施草案，不再提供当前逐行操作步骤。 |
| 怎样处理反馈和网关？ | `docs/feedback_regression.md`、`docs/gateway_evaluation_roadmap.md` | 两者用途独立；实施状态只更新执行清单。 |
| 怎样学习 Agent？ | `docs/learning_current.md` | 两份原学习路线移入 `docs/history/`，旧路径保留导航。 |
| 如何使用独立的前端聊天？ | `../web/my-app/docs/chat_current.md` | 前端原接入教程与执行记录移入其 `docs/history/`。 |

## 分批实施

### 第 1 批：纠正入口与状态，不搬文件

- [x] 新建 `docs/index.md`，按当前实现、记录、学习资料列出文档；README 指向索引。
- [x] 修正 README 中“前端尚未适配”的过期说法，标注前端不在本仓库 Git 提交中。
- [x] `docs/TODO.md` 顶部说明它是工程问题台账，产品任务以 `tasks/todo.md` 为准。
- [x] 两份学习路线与 JWT 原实施草案移入 `docs/history/`，旧路径保留导航。

### 第 2 批：合并正式聊天与会话说明

- [x] 以 `docs/graph.md` 为正式架构入口，补上执行事件、SSE 与历史读取；Graph 迁移记录移入历史目录。
- [x] 新建 `docs/session_current.md` 作为当前数据库契约；首版 DDL 留在设计稿，会话存储交付记录移入历史目录。
- [x] 前端新建 `../web/my-app/docs/chat_current.md`，集中当前请求、会话和执行过程；首版教程及进度记录移入前端历史目录，旧路径保留导航。

### 第 3 批：拆分超长文档

- [x] 从 `docs/auto_recall.md` 提炼当前记忆读写、用户隔离和排障入口至 `docs/memory_current.md`；原理与独立 Graph 练习留原文。
- [x] `docs/jwt_implementation_guide.md` 移入历史目录；当前身份边界由 `docs/jwt_auth.md` 维护。
- [x] 两份旧学习路线移入历史目录，当前阶段与下一步合并至 `docs/learning_current.md`，不拼接旧全文。

### 第 4 批：清理重复与收尾

- [x] `docs/TODO.md` 保持原路径，明确定位为工程问题台账；产品执行状态只在 `tasks/todo.md` 勾选，避免为改名制造额外链接迁移。
- [x] `docs/feedback_regression.md`、`docs/gateway_evaluation_roadmap.md`、`docs/cross_session_memory_acceptance.md` 保留独立；索引和当前说明已给出各自入口。
- [x] `docs/codex_deepseek_fallback.md` 标记为未提交的独立实验，不纳入当前导航；`docs/test.pdf` 由 PDF 解析示例引用且被 Git 忽略，保留为本地示例输入。
- [x] 核对 README、后端和前端文档的相对链接；旧路径均有新入口，历史日期仍可追溯。

迁移分批核对，文档改动统一提交时写明“当前入口”与被标为历史的文档。用户负责真实功能测试；文档整理不触碰运行代码，也不把未验收功能写成已验收。
