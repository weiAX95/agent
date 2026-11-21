"""失联模型调用核对命令；默认只读，执行须提供核对人和证据引用。"""

import argparse
import asyncio
import json
import os
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.usage_storage import list_stale_model_calls, reconcile_stale_model_calls


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="预览或标记已过 Run 截止时间的失联模型调用")
    parser.add_argument("--grace-seconds", type=int, default=300)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewer-id", type=uuid.UUID)
    parser.add_argument("--evidence-ref")
    args = parser.parse_args(argv)
    if args.apply and (args.reviewer_id is None or not args.evidence_ref):
        parser.error("--apply 必须同时提供 --reviewer-id 和 --evidence-ref")
    return args


async def run_command(engine, args, now=None) -> dict:
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        if args.apply:
            call_ids = await reconcile_stale_model_calls(
                db, args.reviewer_id, args.evidence_ref,
                now=now, grace_seconds=args.grace_seconds, limit=args.limit,
            )
            await db.commit()
        else:
            call_ids = await list_stale_model_calls(
                db, grace_seconds=args.grace_seconds, limit=args.limit,
                now=now,
            )
    # 只输出标识，不输出请求正文、供应商响应或数据库连接信息。
    return {"mode": "apply" if args.apply else "preview",
            "count": len(call_ids), "call_ids": [str(item) for item in call_ids]}


async def main():
    args = parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 未配置")
    engine = create_async_engine(url)
    try:
        print(json.dumps(await run_command(engine, args), ensure_ascii=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
