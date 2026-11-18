"""输出待核对 Run 的只读元数据，不输出原始消息或工具结果。"""

import argparse
import asyncio
import json
import os
import uuid

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.run_recovery import inspect_run_recovery
from src.agent.checkpoint import checkpoint_dsn


async def main():
    parser = argparse.ArgumentParser(description="读取待核对 Run 的 checkpoint 元数据")
    parser.add_argument("--run-id", type=uuid.UUID, required=True)
    parser.add_argument("--owner-id", type=uuid.UUID, required=True)
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 未配置")
    engine = create_async_engine(url)
    try:
        async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(url)) as saver:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                snapshot = await inspect_run_recovery(db, saver, args.owner_id, args.run_id)
        print(json.dumps(snapshot, ensure_ascii=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
