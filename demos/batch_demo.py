"""
Agent 项目：批次调用 vs 非批次调用 demo

依赖：
    pip install openai python-dotenv

运行：
    python batch_demo.py

说明：
    - 非批次调用：逐个同步请求大模型，简单直接但耗时。
    - 批量并发调用：使用 asyncio + openai.AsyncOpenAI 同时发起多个请求，
      适合需要立即拿到结果的批量场景。
    - OpenAI Batch API：适合大规模、异步、低成本的批量任务（需要上传 JSONL 文件）。
"""

import asyncio
import os
import time
from pathlib import Path

import dotenv
from openai import AsyncOpenAI, OpenAI

# 加载 .env 配置
dotenv.load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("api-key")
BASE_URL = os.getenv("OPENAI_BASE_URL") or os.getenv("base-url") or "https://api.openai.com/v1"
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# 示例问题列表
QUESTIONS = [
    "请用一句话解释什么是 agent？",
    "什么是大模型幻觉？",
    "agent 的记忆模块有什么作用？",
    "agent 的规划模块有什么作用？",
    "agent 的行动模块有什么作用？",
]


def create_client():
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


def create_async_client():
    return AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)


def non_batch_call(client: OpenAI, questions: list[str]) -> list[str]:
    """非批次调用：逐个同步请求。"""
    answers = []
    for question in questions:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": question}],
            max_tokens=100,
        )
        content = response.choices[0].message.content or ""
        answers.append(content.strip())
    return answers


async def batch_async_call(client: AsyncOpenAI, questions: list[str]) -> list[str]:
    """批次调用：并发异步请求。"""

    async def ask(question: str) -> str:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": question}],
            max_tokens=100,
        )
        return (response.choices[0].message.content or "").strip()

    tasks = [ask(q) for q in questions]
    return await asyncio.gather(*tasks)


def print_results(label: str, questions: list[str], answers: list[str], elapsed: float):
    print(f"\n{'=' * 40}")
    print(f"{label}  耗时: {elapsed:.2f}s")
    print("=" * 40)
    for q, a in zip(questions, answers):
        print(f"Q: {q}")
        print(f"A: {a}\n")


def prepare_batch_jsonl(questions: list[str], output_path: Path):
    """生成 OpenAI Batch API 所需的 .jsonl 文件。"""
    import json

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for idx, question in enumerate(questions):
            line = {
                "custom_id": f"task-{idx}",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": MODEL,
                    "messages": [{"role": "user", "content": question}],
                    "max_tokens": 100,
                },
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return output_path


def batch_api_call(client: OpenAI, questions: list[str]):
    """OpenAI Batch API 调用示例（适合大规模异步批量任务）。"""
    import json

    jsonl_path = Path("tmp/batch_input.jsonl")
    prepare_batch_jsonl(questions, jsonl_path)

    # 1. 上传文件
    with jsonl_path.open("rb") as f:
        file_obj = client.files.create(file=f, purpose="batch")
    print(f"已上传 batch 输入文件: {file_obj.id}")

    # 2. 创建 batch 任务
    batch = client.batches.create(
        input_file_id=file_obj.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )
    print(f"已创建 batch 任务: {batch.id}，状态: {batch.status}")

    # 3. 轮询直到完成（demo 用，生产环境建议用 webhook / 定时任务）
    while True:
        batch = client.batches.retrieve(batch.id)
        print(f"当前状态: {batch.status}")
        if batch.status in {"completed", "failed", "cancelled", "expired"}:
            break
        time.sleep(2)

    if batch.status != "completed":
        print(f"Batch 任务未成功完成: {batch.status}")
        return

    # 4. 下载结果
    result_file = client.files.content(batch.output_file_id)
    result_text = result_file.read().decode("utf-8")
    print("\nBatch API 结果:")
    for line in result_text.strip().split("\n"):
        data = json.loads(line)
        custom_id = data.get("custom_id")
        content = data["response"]["body"]["choices"][0]["message"]["content"]
        print(f"{custom_id}: {content.strip()}")


if __name__ == "__main__":
    sync_client = create_client()
    async_client = create_async_client()

    # 1. 非批次调用
    start = time.perf_counter()
    answers_non_batch = non_batch_call(sync_client, QUESTIONS)
    elapsed_non_batch = time.perf_counter() - start
    print_results("非批次调用（同步逐个）", QUESTIONS, answers_non_batch, elapsed_non_batch)

    # 2. 批次调用：并发异步
    start = time.perf_counter()
    answers_batch_async = asyncio.run(batch_async_call(async_client, QUESTIONS))
    elapsed_batch_async = time.perf_counter() - start
    print_results("批次调用（asyncio 并发）", QUESTIONS, answers_batch_async, elapsed_batch_async)

    # 3. OpenAI Batch API（默认注释掉，避免误触发长时间任务）
    # print("\n开始 OpenAI Batch API 调用...")
    # batch_api_call(sync_client, QUESTIONS)
