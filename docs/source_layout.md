# `src` 目录与代码入口

更新日期：2026-10-31。本页记录本次目录整理后的用途；正式聊天从 `api.server` 进入。

```text
src/
├── agent/
│   ├── main.py             主 Agent 构建、聊天与流式 checkpoint 收尾
│   ├── chat_workflow.py    单轮召回、回答、记忆写入的业务图
│   ├── research_agent.py   检索子 Agent
│   ├── progress_tool.py    主 Agent 的可见进度工具
│   ├── checkpoint.py       会话线程和锁的标识
│   ├── llm.py              正式模型配置；启动时创建
│   ├── web_tools.py        网页与知识库检索工具
│   ├── tools.py            旧导入路径的兼容转发
│   └── memory/
│       ├── client.py       启动时创建 Embedding、Milvus 和 collection
│       ├── context.py      回答前召回与本轮系统提示
│       ├── decisions.py    召回路由、事实提取和冲突决策
│       └── store.py        长期记忆的查询、插入、替换
└── parsers/
    └── pdf_parser.py       PDF 解析，与聊天业务独立

examples/agent/              独立 Graph 与演示脚本，不是正式 API
```

`api/server.py` 继续从 `src.agent.main` 导入 `build_agent` 和 `chat_stream`，接口和数据库结构未因本次整理改变。`build_agent()` 在应用启动时初始化正式模型、Embedding、Milvus 和记忆决策链；关闭应用时释放资源。单纯导入正式模块不连接外部服务。主 Agent 通过 `research` 工具把查询交给子 Agent；记忆在业务图中通过 `memory` 模块处理。`src.agent.tools` 继续转发原有名称，供旧脚本导入；新代码应直接引用对应模块。

从项目根目录运行学习示例时使用模块名，例如 `python -m examples.agent.demos.memory_graph_demo`。部分演示会调用真实模型或改写 Milvus 数据，执行前先阅读脚本。原有离线测试中的文件路径已更新为新目录；本次整理未执行测试，由用户按需验证。
