# 当前长期记忆机制

更新日期：2026-10-29。正式入口是 [聊天业务图](graph.md)；本文只描述 `/api/chat` 当前使用的记忆链路。原理、向量参数和独立 Graph 练习保留在 [自动召回学习文档](auto_recall.md)。

## 两种状态

主 Agent 的 PostgreSQL checkpoint 保存**本会话上下文**，按用户与会话组合隔离；Milvus 保存**同一用户可跨会话使用的长期事实**。新建 session 不会清除长期事实，也不会让不同 session 共享 checkpoint。

## 一轮聊天的读写顺序

1. `chat_workflow.py` 的 `recall` 节点调用 `main.py` 的召回逻辑。先判断本轮是否需要个人记忆；明确类别时按用户和 `memory_key` 读取，类别不确定且确有召回需求时才使用向量检索兜底。
2. 召回结果只进入本轮动态系统提示，主 Agent 仍只接收当前用户消息。未找到结果时不推断用户个人事实。
3. 回答后，`judge` 从当前用户消息提取明确、可长期使用的事实；`resolve` 比较同用户同类别旧记录；`persist` 执行新增、替换或忽略。纯提问、引用、假设和第三人称陈述不应当成用户事实。
4. `chat_messages.memory_status` 记录 `saved`、`unchanged`、`failed` 或 `skipped` 等结果；聊天历史可显示本轮状态。回答生成成功不等于记忆写入成功。

姓名 `user_name`、希望被称呼的方式 `preferred_name` 与饮食偏好 `food_preference` 属于不同类别。“我叫什么”这类明确问句按姓名类别读取；改名走旧记录冲突决策。用户身份来自 JWT，不能由工具调用者随意传入其他用户 ID。

当前实现位置：[业务图](../src/agent/chat_workflow.py)、[召回和提取](../src/agent/main.py)、[Milvus 工具](../src/agent/tools.py)。具体跨会话场景见 [验收记录](cross_session_memory_acceptance.md)。真实模型的提取与召回效果尚需用户按需验收。
