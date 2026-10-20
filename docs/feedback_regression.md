# 用户反馈到回归集

这条流程处理已完成回答中的错误反馈。`my-app` 的回答气泡提供“回答有误 / 记忆有误”；`/chat` 的 `done` / `stopped` 事件带 `chat_log_id`，前端以当前 Bearer 身份调用 `POST /api/feedback`。服务端只保存聊天记录 ID、反馈类别和提交用户，先核对该聊天记录归当前用户所有。反馈不是评测真值，仍须人工审核。

## 审核与脱敏

1. 在项目根目录运行 `python -m tools.feedback_loop export --out output/feedback-reviews.jsonl`。导出文件含原始问题与回答，只允许写入被 Git 忽略的 `output/`，由私有文件权限保护；不要发送到公共日志或提交到仓库。`--limit`、`--offset` 可分页。
2. 逐条检查问题类型、预期行为和可复现性。删除姓名、联系方式、会话标识、密钥等私人内容，把问题重写为不依赖真实用户数据的样本；不适合纳入回归集的记录设为 `"decision":"rejected"`。
3. 对通过审核的记录设置唯一 `case_id`、`scenario`、`expected`，设 `"decision":"approved"` 与 `"redacted":true`。`observed_answer` 仅供审核，提升时不会写入数据集。`expected` 支持 `status`（`done` / `stopped` / `error`）、`contains_all`、`excludes_all`；这些是确定性检查，语义质量仍需人工或独立评审。
4. 执行 `python -m tools.feedback_loop promote --reviews output/feedback-reviews.jsonl --dataset evals/regression.jsonl`。同一反馈重复导入会跳过；ID 冲突、未脱敏的批准项会失败，且不会部分写入。

## 与基线比较

在隔离的测试环境和专用测试用户中对同一数据集运行当前 Agent。`run` 会给每条样本生成新会话，但长期记忆仍按用户共享；涉及记忆的样本应使用独立用户或在每轮实验前恢复同一数据库快照，避免样本相互污染。令牌通过环境变量传入，绝不放进命令参数或报告：

```bash
export EVAL_ACCESS_TOKEN='仅供隔离评测用户使用的短期令牌'
python -m tools.feedback_loop run --dataset evals/regression.jsonl --base-url http://127.0.0.1:8001 --out output/current-observations.jsonl
```

每个样本产出一条 JSONL 观察结果。格式示例：

```json
{"case_id":"recall-001","status":"done","answer":"你说过喜欢茶。","latency_ms":820,"input_tokens":90,"output_tokens":14}
```

观察结果只允许写入被 Git 忽略的 `output/`，其中含模型原始回答。用项目当前代码和模型版本打分：

```bash
python -m tools.feedback_loop score --dataset evals/regression.jsonl --observations output/baseline-observations.jsonl --code-version BASE_COMMIT --model BASE_MODEL --out output/baseline.json
python -m tools.feedback_loop score --dataset evals/regression.jsonl --observations output/current-observations.jsonl --code-version CURRENT_COMMIT --model CURRENT_MODEL --out output/current.json
python -m tools.feedback_loop compare --baseline output/baseline.json --current output/current.json --out output/comparison.md
```

`compare` 要求数据集摘要和样本 ID 完全相同；有任一原本通过的样本回退，就以退出码 1 结束并在报告中列出 ID。报告不复制原始回答。数据集新增样本时，先在新数据集上重跑两版，再比较。记录模型、提示词、召回阈值和代码版本，人工复核语言质量与波动。当前命令只完成审核闸门、确定性打分和版本对比；真实模型运行及语义评分仍由隔离评测流程提供，不能把离线单元测试当成真实质量基线。

## 边界

- `ChatFeedback` 是新表，现有部署需重启 API 触发当前 `create_all` 建表；正式生产部署应改为版本化迁移。
- 旧聊天记录的 SSE 响应没有 `chat_log_id`，前端只在新完成事件中拿到该 ID 时展示反馈按钮。
- 前端只提交记录 ID 和类别，不二次发送问题或回答正文；审核导出仍含原始内容，应限制访问和留存。
- `evals/regression.jsonl` 在首条脱敏反馈获批后建立；没有真实观察结果时，不声明已有模型质量基线。

## 本次修改位置与理由

| 位置 | 修改逻辑 | 验证 |
| --- | --- | --- |
| `api/models.py`、`api/routers/feedback.py` | 新反馈表只保存归属、聊天记录 ID 和类别；按 JWT 用户核对聊天记录，重复反馈返回 409 | 离线所有权/重复测试；本地 API 以隔离记录验证 401、404、201、409，随后清理记录 |
| `api/server.py` | 在聊天日志提交后，将该日志 ID 附加到 `done` / `stopped`；失败时仍发送原有 `error` | `test_checkpoint_flow.py`、`test_stream_recovery.py` |
| `../../web/my-app/src/lib/api.ts`、`../../web/my-app/src/App.tsx` | 页面只在拿到本轮日志 ID 后展示反馈动作；提交失败可重试 | `npm run build`、`npm run lint` |
| `tools/feedback_loop.py` | 私有导出 → 人工批准/脱敏 → 回归集 → 隔离 API 运行 → 确定性打分 → 逐例基线对比 | `test_feedback_loop.py`；CLI 在回退时退出码为 1 |
| `docs/TODO.md`、CI | 已有证据的实现标记完成，仍缺真实质量报告的部分保留待办；把新测试纳入离线工作流 | 定向离线测试和文档链接检查 |
