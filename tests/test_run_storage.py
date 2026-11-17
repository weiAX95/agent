"""M2 第一段：用真实 SQL 条件更新验证 Run 的幂等终态。"""

import unittest
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.models import AgentRun
from api.run_storage import new_run, record_run_step, transition_run
from src.agent.run_policy import RunLimits, RunStatus


class RunStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            # SQLite 离线测试只验证 SQL 转换；PostgreSQL 外键及竞争需另行验收。
            for name in ("users", "chat_sessions", "chat_messages"):
                await connection.exec_driver_sql(f"CREATE TABLE {name} (id CHAR(32) PRIMARY KEY)")
            await connection.run_sync(AgentRun.__table__.create)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_deadline_and_single_terminal_cas(self):
        now = datetime.now(UTC)
        run = new_run(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(),
                      RunLimits(max_steps=3, timeout_seconds=10), now)
        self.assertEqual(run.deadline_at, now + timedelta(seconds=10))
        async with self.sessions() as db:
            db.add(run)
            await db.commit()
            self.assertTrue(await transition_run(db, run.id, RunStatus.CREATED, RunStatus.RUNNING))
            await db.commit()
            self.assertFalse(await transition_run(db, run.id, RunStatus.CREATED, RunStatus.RUNNING))
            self.assertTrue(await transition_run(db, run.id, RunStatus.RUNNING, RunStatus.CANCELLED))
            await db.commit()
            self.assertFalse(await transition_run(db, run.id, RunStatus.RUNNING, RunStatus.SUCCEEDED))
            await db.refresh(run)
            self.assertEqual(run.status, RunStatus.CANCELLED)
            self.assertIsNotNone(run.finished_at)

    async def test_invalid_transition_rejected(self):
        async with self.sessions() as db:
            with self.assertRaises(ValueError):
                await transition_run(db, uuid.uuid4(), RunStatus.CREATED, RunStatus.SUCCEEDED)

    async def test_step_cas_never_double_counts(self):
        run = new_run(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(),
                      RunLimits(max_steps=2))
        async with self.sessions() as db:
            db.add(run)
            await db.commit()
            self.assertTrue(await transition_run(db, run.id, RunStatus.CREATED, RunStatus.RUNNING))
            self.assertTrue(await record_run_step(db, run.id, 1))
            self.assertFalse(await record_run_step(db, run.id, 1))
            self.assertTrue(await record_run_step(db, run.id, 2))
            self.assertFalse(await record_run_step(db, run.id, 3))
            await db.commit()
            await db.refresh(run)
            self.assertEqual(run.steps, 2)


if __name__ == "__main__":
    unittest.main()
