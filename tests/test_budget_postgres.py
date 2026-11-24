"""一次性 PostgreSQL 预算迁移与跨会话并发预留验证。"""

import asyncio
import os
import unittest
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.budget_storage import reserve_run_budget, settle_run_budget_from_usage
from api.models import (
    AgentRun, ChatMessage, ChatSession, GatewayBudgetAccount,
    GatewayBudgetAdjustment, GatewayBudgetReservation, GatewayModelCall,
    GatewayPriceVersion, GatewayUsageEvent, User,
)
from api.run_storage import new_run, transition_run
from api.usage_storage import record_usage_receipt, start_model_call
from api.usage_attempt_sink import DatabaseAttemptSink
from src.agent.budget_policy import BudgetExceeded
from src.agent.physical_transport import MeteredRun
from src.agent.run_policy import RunLimits, RunStatus
from src.agent.usage_pricing import TokenUsage


DB_URL = os.getenv("BUDGET_TEST_POSTGRES_URL")


@unittest.skipUnless(
    DB_URL and os.getenv("BUDGET_TEST_POSTGRES_ALLOW_REBUILD") == "1",
    "仅在一次性 gateway_m4_test 数据库运行",
)
class BudgetPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_migration_and_parallel_reservation(self):
        if make_url(DB_URL).database != "gateway_m4_test":
            raise ValueError("预算并发测试仅允许 gateway_m4_test 数据库")
        engine = create_async_engine(DB_URL)
        try:
            async with engine.begin() as connection:
                for table in (User.__table__, ChatSession.__table__, ChatMessage.__table__,
                              AgentRun.__table__, GatewayPriceVersion.__table__,
                              GatewayModelCall.__table__, GatewayUsageEvent.__table__):
                    await connection.run_sync(table.create, checkfirst=True)
                migration = Path("migrations/20261124_gateway_budget_reservations.sql").read_text()
                for _ in range(2):
                    for statement in migration.split(";"):
                        if statement.strip() and "CREATE " in statement:
                            await connection.exec_driver_sql(statement.strip())
                for name in ("gateway_budget_accounts", "gateway_budget_reservations",
                             "gateway_budget_adjustments"):
                    self.assertIsNotNone(await connection.scalar(text(
                        "SELECT to_regclass(:name)"), {"name": f"public.{name}"}))

            sessions = async_sessionmaker(engine, expire_on_commit=False)
            owner, session_id = uuid.uuid4(), uuid.uuid4()
            today = datetime.now(UTC).date()
            async with sessions() as db:
                db.add(User(id=owner, username=f"m4-{owner}", hashed_password="test"))
                await db.flush()
                db.add(ChatSession(id=session_id, owner_id=owner,
                                   title="M4 并发", checkpoint_thread_id=str(session_id)))
                await db.flush()
                run_ids = []
                for seq in (1, 2):
                    request_id, message_id = uuid.uuid4(), uuid.uuid4()
                    db.add(ChatMessage(id=message_id, session_id=session_id,
                                       request_id=request_id, seq=seq, role="assistant",
                                       status="streaming", content=""))
                    await db.flush()
                    run = new_run(owner, session_id, request_id, message_id, RunLimits())
                    db.add(run)
                    run_ids.append(run.id)
                account = GatewayBudgetAccount(
                    id=uuid.uuid4(), owner_id=owner, currency="USD",
                    budget_date=today, limit_amount=Decimal("10"),
                )
                db.add(account)
                await db.commit()

            async def claim(run_id):
                async with sessions() as db:
                    try:
                        reservation = await reserve_run_budget(
                            db, run_id, owner, "USD", today, Decimal("7"),
                        )
                        await db.commit()
                        return reservation.id
                    except BudgetExceeded as exc:
                        await db.rollback()
                        return exc.code

            outcomes = await asyncio.gather(*(claim(run_id) for run_id in run_ids))
            self.assertEqual(sum(isinstance(item, uuid.UUID) for item in outcomes), 1)
            self.assertIn(BudgetExceeded.code, outcomes)
            winner = run_ids[0] if isinstance(outcomes[0], uuid.UUID) else run_ids[1]
            repeated = await asyncio.gather(claim(winner), claim(winner))
            self.assertEqual(repeated, [next(item for item in outcomes
                                              if isinstance(item, uuid.UUID))] * 2)
            async with sessions() as db:
                stored = await db.get(GatewayBudgetAccount, account.id)
                self.assertEqual(stored.reserved_amount, Decimal("7"))
                rows = (await db.scalars(select(GatewayBudgetReservation).where(
                    GatewayBudgetReservation.account_id == account.id,
                ))).all()
                self.assertEqual(len(rows), 1)

            async with sessions() as db:
                db.add(GatewayPriceVersion(
                    id=f"m4-{winner}", provider="test", model="m4", currency="USD",
                    input_per_million=Decimal("1"), output_per_million=Decimal("0"),
                    cached_input_per_million=Decimal("0"),
                    effective_at=datetime.now(UTC),
                ))
                await db.flush()
                call = await start_model_call(db, winner, owner, "test", "m4")
                await transition_run(db, winner, RunStatus.CREATED, RunStatus.RUNNING)
                await record_usage_receipt(db, call.id, "actual", "succeeded",
                                           TokenUsage(2_000_000, 0, 0), "provider")
                await transition_run(db, winner, RunStatus.RUNNING, RunStatus.SUCCEEDED)
                await db.commit()

            sync_engine = create_engine(make_url(DB_URL).set(drivername="postgresql+psycopg"))
            try:
                sink = DatabaseAttemptSink(sync_engine, sessions)
                with self.assertRaisesRegex(ValueError, "不可再发起"):
                    sink.start(MeteredRun(winner, owner), "test", "m4")
            finally:
                sync_engine.dispose()

            async def settle():
                async with sessions() as db:
                    result = await settle_run_budget_from_usage(db, winner, owner)
                    await db.commit()
                    return result.confirmed_cost

            self.assertEqual(await asyncio.gather(settle(), settle()),
                             [Decimal("2"), Decimal("2")])
            async with sessions() as db:
                stored = await db.get(GatewayBudgetAccount, account.id)
                self.assertEqual((stored.spent_amount, stored.reserved_amount),
                                 (Decimal("2"), Decimal("0")))
                adjustments = (await db.scalars(select(GatewayBudgetAdjustment).where(
                    GatewayBudgetAdjustment.reservation_id == rows[0].id,
                ))).all()
                self.assertEqual(len(adjustments), 1)
        finally:
            await engine.dispose()
