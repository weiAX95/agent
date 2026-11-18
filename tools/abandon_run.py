"""人工核对后的 Run 放弃命令；只允许有数据库权限的运维人员执行。"""

import argparse
import asyncio
import json
import os
import uuid

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.run_recovery import abandon_run_after_review
from src.agent.checkpoint import checkpoint_dsn


def parse_args():
    parser = argparse.ArgumentParser(description="核对外部结果后封存中断 Run，旧会话保持阻断")
    parser.add_argument("--run-id", type=uuid.UUID, required=True)
    parser.add_argument("--owner-id", type=uuid.UUID, required=True)
    parser.add_argument("--reviewer-id", type=uuid.UUID, required=True)
    checkpoint = parser.add_mutually_exclusive_group(required=True)
    checkpoint.add_argument("--checkpoint-id")
    checkpoint.add_argument("--no-checkpoint", action="store_true")
    parser.add_argument("--external-effect-status", choices=("none", "confirmed"), required=True)
    parser.add_argument("--evidence-ref", required=True)
    parser.add_argument("--confirm-abandon", action="store_true", required=True)
    return parser.parse_args()


async def main():
    args = parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 未配置")
    engine = create_async_engine(url)
    try:
        async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(url)) as saver:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                outcome = await abandon_run_after_review(
                    db, saver, args.owner_id, args.run_id, args.reviewer_id,
                    None if args.no_checkpoint else args.checkpoint_id,
                    args.external_effect_status, args.evidence_ref,
                )
        print(json.dumps(outcome, ensure_ascii=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
