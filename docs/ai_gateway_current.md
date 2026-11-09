# 聊天产品 AI 网关：当前实现

本页只描述主聊天产品的入口和模型调用层。`tools/codex_deepseek/` 是独立的 Codex 适配器，不进入 `/api/chat` 链路。

## 请求链路

客户端从 `GET /api/models` 读取可选档位，发送 `POST /api/chat`（可带 `model_profile`）→ 请求体上限 256 KiB → JWT 与会话所有权校验 → 同会话执行锁和 `request_id` 幂等 → 所选档位的 Agent → 对应 OpenAI 兼容上游 → SSE 和数据库终态。所有档位共用同一会话 checkpoint；研究子 Agent 与记忆决策使用本轮所选模型。

- `api/gateway.py` 在解析 JSON 前限制请求体；超过上限返回 HTTP 413。每个聊天响应带 `X-Trace-ID`，包括解析前拒绝的请求，便于定位入口错误。
- `src/agent/model_gateway.py` 校验模型名、服务地址、密钥、超时和重试配置。密钥不写入日志。所有主 Agent、research 子 Agent 和记忆决策共用 `get_llm()` 构造的模型。
- 默认模型调用超时 60 秒、SDK 重试 0 次；可配置超时 5–300 秒、重试 0–2 次。重试只作用于单次上游模型请求，不重新执行整个 Agent 或写入工具。流式请求建议保持 0，避免上游重试语义不清时重复片段。
- 已有 `request_id` 保证相同正文与同一模型档位重放结果；同键更换档位返回 409。同会话执行互斥。生成失败会持久化失败状态，不会把半截回答记为成功。旧消息映射到当前默认档位。
- 上游超时、429、连接错误/5xx 和鉴权失败分别映射为 `MODEL_TIMEOUT`、`MODEL_RATE_LIMITED`、`MODEL_UNAVAILABLE`、`MODEL_AUTH_FAILED`；框架包装的异常会沿因果链检查。SSE `error` 与同一 `request_id` 的重放使用相同的安全提示；服务器日志保留异常堆栈和 `request_id`。
- 成功与失败请求日志记录 `request_id`、状态、总耗时和首 token 耗时；现有 `usage` SSE 事件只使用服务商报告的 token 数，不推算缺失用量。

## 环境配置

```dotenv
OPENAI_API_KEY=your_key
OPENAI_BASE_URL=https://your-model-api.example/v1
OPENAI_MODEL=your_model
MODEL_TIMEOUT_SECONDS=60
MODEL_MAX_RETRIES=0
```

多上游时设置服务端 JSON 档位和默认档位；浏览器只读取档位 ID、模型名，不接收密钥或密钥环境变量名：

```dotenv
MODEL_UPSTREAMS_JSON='{"primary":{"model":"model-a","base_url":"https://provider-a.example/v1","api_key_env":"PROVIDER_A_API_KEY"},"secondary":{"model":"model-b","base_url":"https://provider-b.example/v1","api_key_env":"PROVIDER_B_API_KEY"}}'
MODEL_DEFAULT_PROFILE=primary
PROVIDER_A_API_KEY=your_first_key
PROVIDER_B_API_KEY=your_second_key
```

当前项目可在保留原有 `OPENAI_MODEL`、`OPENAI_BASE_URL`、`OPENAI_API_KEY` 的同时，仅追加 DeepSeek 档位：

```dotenv
MODEL_UPSTREAMS_JSON='{"deepseek":{"model":"deepseek-flash","base_url":"https://api.deepseek.com","api_key_env":"DEEPSEEK_API_KEY"}}'
MODEL_DEFAULT_PROFILE=default
DEEPSEEK_API_KEY=your_deepseek_key
```

已有 OpenAI 配置会自动成为 `default` 档位；未设置 `MODEL_UPSTREAMS_JSON` 时仍只使用这个档位。当前 DeepSeek 模型名和地址以[官方接入文档](https://api-docs.deepseek.com/guides/codex)为准。所有档位在服务启动时校验和创建；任一档位配置无效会阻止启动。切换档位只影响新一轮请求，不自动重跑先前的工具或回答。

`OPENAI_BASE_URL` 接受 HTTP(S) 服务地址，不接受内嵌用户名、密码、查询串或片段。HTTP 仅用于可信的本地模型服务；生产环境应使用 HTTPS。切换模型前需核对工具调用与结构化输出兼容性。

## 后续边界

当前未做跨会话、跨 worker 的用户速率限制；同会话锁不是全局限流。尚无按 provider/model 聚合的指标、费用估算或自动模型降级。上线前需在目标部署环境选择共享限流存储或入口代理，并建立模型故障与费用基线。真实端到端验收由用户安排。
