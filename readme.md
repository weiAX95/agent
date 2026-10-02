# LLM Agent 项目

> 大模型幻觉研究 · Agent 核心模块：记忆 / 规划 / 行动 / 工具

## 项目文档

- [问题与改进 TODO](docs/TODO.md)：已确认问题、修复顺序、验收条件，以及原有开发计划的状态。
- [自动召回学习文档](docs/auto_recall.md)：结合当前代码讲解长期记忆读取、向量检索、Graph 改造和离线实验。
- [学习进度路线（执行版）](docs/learning_progress_roadmap.md)：基于当前源码制定的分阶段学习地图、每周任务、练习和验收标准。
- [Agent 学习路线](docs/agent_learning_roadmap.md)：整体学习计划；部分实现描述较早，记忆模块现状请结合上述文档与源码阅读。

---

## 目录结构

```
agent/
├── .env                        # 环境变量（API Key，不提交到 Git）
├── readme.md
│
├── src/                        # 核心源码
│   ├── agent/
│   │   ├── main.py             # Agent 主入口（LangChain Tool-Call 循环）
│   │   └── tools.py            # Agent 工具集（联网搜索、知识库检索）
│   └── parsers/
│       └── pdf_parser.py       # PDF 解析器（基于 MinerU HTTP API）
│
├── demos/                      # 学习示例
│   ├── batch_demo.py           # 批量调用 vs 非批量调用对比
│   ├── prompt_template_demo.py # Prompt Template 三种用法演示
│   ├── runnable_branch_demo.py # LangChain RunnableBranch 条件路由
│   └── parse_txt.py            # 文本文件加载示例
│
├── scripts/                    # 数据库管理脚本
│   └── milvus/
│       ├── create_db.py        # 创建 Milvus Collection
│       ├── insert_db.py        # 文档向量化并写入 Milvus
│       ├── query_db.py         # 查询 Collection 数据
│       ├── search_db.py        # 向量相似度搜索
│       ├── delete_data.py      # 删除指定数据
│       └── quick_start.py      # Milvus 快速上手示例
│
├── projects/                   # 独立子项目
│   └── crypto_bot/
│       └── crypto_trading_bot.py  # 比特币 AI 合约交易机器人（仅供学习）
│
├── tests/                      # 测试脚本
│   ├── test_api.py             # 阿里百炼 API 测试
│   ├── test_gemini.py          # Google Gemini API 测试
│   └── test_search.py          # 网络搜索接口测试
│
├── notebooks/                  # Jupyter 笔记本
│   └── api_key.ipynb           # API Key 管理
│
├── web/                        # HTML 可视化页面
│   ├── index.html
│   ├── runnable_branch_visual.html
│   └── 重大隐患练习.html
│
├── assets/                     # 静态资源文件
│   └── sample.docx
│
├── docs/                       # 文档资料
│   └── test.pdf
│
├── data/                       # 本地数据
│   └── milvus_demo.db/         # Milvus Lite 向量数据库
│
└── output/                     # 运行输出（PDF 解析结果等）
```

---

## 快速开始

```bash
# 激活虚拟环境
source .venv/bin/activate

# 运行 Agent 主程序
python src/agent/main.py

# 初始化 Milvus 数据库
python scripts/milvus/create_db.py

# 插入文档数据
python scripts/milvus/insert_db.py

# 向量搜索
python scripts/milvus/search_db.py
```

---

## 环境变量

在 `.env` 中配置以下变量：

```
OPENAI_API_KEY=your_key
OPENAI_API_BASE_URL=https://...
OPENAI_MODEL=qwen-plus
GEMINI_API_KEY=your_key
GEMINI_MODEL=gemini-2.0-flash
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
```

freeze > requirements.txt
