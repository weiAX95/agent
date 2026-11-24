"""预算预留只能在已有账户内执行，并保持同一 Run 幂等。"""

import unittest
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.base import Base
from api.budget_storage import BudgetNotConfigured, reserve_run_budget
from api.models import (
    ChatMessage, ChatSession, GatewayBudgetAccount, GatewayBudgetReservation, User,
)
from api.run_storage import new_run
from src.agent.budget_policy import BudgetExceeded
from src.agent.run_policy import RunLimits


class BudgetStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.owner, self.other, self.session_id = (uuid.uuid4() for _ in range(3))
        self.today = datetime.now(UTC).date()
        self.sequence = 0
        async with self.sessions() as db:
            db.add_all([
                User(id=self.owner, username="budget-owner", hashed_password="test"),
                User(id=self.other, username="budget-other", hashed_password="test"),
            ])
            await db.flush()
            db.add(ChatSession(id=self.session_id, owner_id=self.owner,
                               title="预算测试", checkpoint_thread_id=str(self.session_id)))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def add_run(self):
        self.sequence += 1
        request_id, message_id = uuid.uuid4(), uuid.uuid4()
        async with self.sessions() as db:
            db.add(ChatMessage(id=message_id, session_id=self.session_id,
                               request_id=request_id, seq=self.sequence, role="assistant",
                               status="streaming", content=""))
            await db.flush()
            run = new_run(self.owner, self.session_id, request_id, message_id, RunLimits())
            db.add(run)
            await db.commit()
            return run.id

    async def add_account(self, limit="10"):
        async with self.sessions() as db:
            account = GatewayBudgetAccount(
                id=uuid.uuid4(), owner_id=self.owner, currency="USD",
                budget_date=self.today, limit_amount=Decimal(limit),
            )
            db.add(account)
            await db.commit()
            return account.id

    async def test_same_run_is_idempotent_and_other_run_sees_reservation(self):
        account_id = await self.add_account()
        first_run, second_run = await self.add_run(), await self.add_run()
        async with self.sessions() as db:
            first = await reserve_run_budget(db, first_run, self.owner, "USD",
                                             self.today, Decimal("7"))
            await db.commit()
        async with self.sessions() as db:
            repeated = await reserve_run_budget(db, first_run, self.owner, "USD",
                                                self.today, Decimal("7"))
            self.assertEqual(repeated.id, first.id)
            with self.assertRaises(BudgetExceeded):
                await reserve_run_budget(db, second_run, self.owner, "USD",
                                         self.today, Decimal("4"))
            await db.rollback()
            self.assertEqual((await db.get(GatewayBudgetAccount, account_id)).reserved_amount,
                             Decimal("7"))
            self.assertIsNone(await db.scalar(select(GatewayBudgetReservation).where(
                GatewayBudgetReservation.run_id == second_run,
            )))

    async def test_missing_account_wrong_owner_and_changed_amount_rejected(self):
        run_id = await self.add_run()
        async with self.sessions() as db:
            with self.assertRaises(BudgetNotConfigured):
                await reserve_run_budget(db, run_id, self.owner, "USD",
                                         self.today, Decimal("1"))
            with self.assertRaises(ValueError):
                await reserve_run_budget(db, run_id, self.other, "USD",
                                         self.today, Decimal("1"))
        await self.add_account()
        async with self.sessions() as db:
            await reserve_run_budget(db, run_id, self.owner, "USD", self.today, Decimal("1"))
            await db.commit()
        async with self.sessions() as db:
            with self.assertRaises(ValueError):
                await reserve_run_budget(db, run_id, self.owner, "USD",
                                         self.today, Decimal("2"))
            with self.assertRaises(ValueError):
                await reserve_run_budget(db, run_id, self.owner, "USD",
                                         self.today, 1.0)
