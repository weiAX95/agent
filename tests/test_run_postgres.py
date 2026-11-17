"""真实 PostgreSQL Run 迁移与并发测试；只在一次性测试数据库中运行。"""

import asyncio
import os
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.models import AgentRun, ChatMessage, ChatSession, User
from api.run_storage import new_run, transition_run
from src.agent.run_policy import RunLimits, RunStatus


DB_URL = os.getenv("RUN_TEST_POSTGRES_URL")


@unittest.skipUnless(
    DB_URL and os.getenv("RUN_TEST_POSTGRES_ALLOW_REBUILD") == "1",
    "设置 RUN_TEST_POSTGRES_URL 与 RUN_TEST_POSTGRES_ALLOW_REBUILD=1 指向一次性测试库",
)
class RunPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 测试会重建表；必须使用名称明确的隔离测试库，绝不碰业务数据库。
        if make_url(DB_URL).database != "gateway_m2_test":
            raise ValueError("Run PostgreSQL 测试仅允许 gateway_m2_test 数据库")
        self.engine = create_async_engine(DB_URL)
        async with self.engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS public.agent_runs"))
            for table in (ChatMessage.__table__, ChatSession.__table__, User.__table__):
                await connection.run_sync(table.drop, checkfirst=True)
            for table in (User.__table__, ChatSession.__table__, ChatMessage.__table__):
                await connection.run_sync(table.create)
            migration = Path("migrations/20261117_agent_runs.sql").read_text()
            await connection.exec_driver_sql(migration)
            await connection.exec_driver_sql(migration)
            self.assertIsNotNone(await connection.scalar(text("SELECT to_regclass('public.agent_runs')")))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.owner, self.session_id, self.message_id, self.request_id = (
            uuid.uuid4() for _ in range(4)
        )
        async with self.sessions() as db:
            db.add(User(id=self.owner, username=f"run-{self.owner}", hashed_password="test"))
            await db.flush()
            db.add(ChatSession(id=self.session_id, owner_id=self.owner,
                               title="测试", checkpoint_thread_id=str(self.session_id)))
            await db.flush()
            db.add(ChatMessage(id=self.message_id, session_id=self.session_id,
                               request_id=self.request_id, seq=1, role="assistant",
                               status="streaming", content=""))
            # ORM 依赖次序由 flush 显式保证，避免测试与生产写入顺序不同。
            await db.flush()
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_migration_and_terminal_cas_under_concurrency(self):
        run = new_run(self.owner, self.session_id, self.request_id, self.message_id,
                      RunLimits(max_steps=2, timeout_seconds=10))
        async with self.sessions() as db:
            db.add(run)
            await db.commit()
            self.assertTrue(await transition_run(db, run.id, RunStatus.CREATED, RunStatus.RUNNING))
            await db.commit()

        async def finish(target):
            async with self.sessions() as db:
                won = await transition_run(db, run.id, RunStatus.RUNNING, target)
                await db.commit()
                return won

        outcomes = await asyncio.gather(finish(RunStatus.SUCCEEDED),
                                        finish(RunStatus.CANCELLED))
        self.assertEqual(sorted(outcomes), [False, True])
        async with self.sessions() as db:
            stored = await db.scalar(select(AgentRun).where(AgentRun.id == run.id))
            self.assertIn(stored.status, {RunStatus.SUCCEEDED, RunStatus.CANCELLED})
            self.assertIsNotNone(stored.finished_at)
            self.assertGreater(stored.deadline_at.replace(tzinfo=UTC), datetime.now(UTC))

    async def test_worker_loss_keeps_run_and_request_unique(self):
        run = new_run(self.owner, self.session_id, self.request_id, self.message_id,
                      RunLimits(max_steps=2, timeout_seconds=10))
        async with self.sessions() as db:
            db.add(run)
            await db.commit()
            self.assertTrue(await transition_run(db, run.id, RunStatus.CREATED, RunStatus.RUNNING))
            await db.commit()
        # 新连接模拟 Worker 重启：不重放模型，只读取尚未裁决的执行事实。
        async with self.sessions() as recovered:
            stored = await recovered.get(AgentRun, run.id)
            self.assertEqual(stored.status, RunStatus.RUNNING)
            self.assertEqual(stored.request_id, self.request_id)
            recovered.add(new_run(self.owner, self.session_id, self.request_id,
                                  self.message_id, RunLimits()))
            with self.assertRaises(IntegrityError):
                await recovered.commit()
            await recovered.rollback()


if __name__ == "__main__":
    unittest.main()
