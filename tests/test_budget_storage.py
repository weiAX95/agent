"""预算预留只能在已有账户内执行，并保持同一 Run 幂等。"""

import unittest
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.base import Base
from api.budget_storage import (
    BudgetNotConfigured, reserve_run_budget, settle_run_budget_from_usage,
)
from api.models import (
    ChatMessage, ChatSession, GatewayBudgetAccount, GatewayBudgetAdjustment,
    GatewayBudgetReservation, GatewayPriceVersion, User,
)
from api.run_storage import new_run, transition_run
from api.usage_storage import record_usage_receipt, start_model_call
from src.agent.budget_policy import BudgetExceeded
from src.agent.run_policy import RunLimits, RunStatus
from src.agent.usage_pricing import TokenUsage


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

    async def test_settlement_and_late_correction_are_idempotent(self):
        account_id, run_id = await self.add_account(), await self.add_run()
        async with self.sessions() as db:
            db.add(GatewayPriceVersion(
                id="budget-usd", provider="test", model="budget", currency="USD",
                input_per_million=Decimal("1"), output_per_million=Decimal("0"),
                cached_input_per_million=Decimal("0"),
                effective_at=datetime.now(UTC),
            ))
            await db.flush()
            await reserve_run_budget(db, run_id, self.owner, "USD", self.today, Decimal("7"))
            call = await start_model_call(db, run_id, self.owner, "test", "budget")
            await transition_run(db, run_id, RunStatus.CREATED, RunStatus.RUNNING)
            await record_usage_receipt(db, call.id, "first", "succeeded",
                                       TokenUsage(2_000_000, 0, 0), "provider")
            still_reserved = await settle_run_budget_from_usage(db, run_id, self.owner)
            self.assertEqual(still_reserved.status, "reserved")
            await transition_run(db, run_id, RunStatus.RUNNING, RunStatus.SUCCEEDED)
            await db.commit()
        async with self.sessions() as db:
            settled = await settle_run_budget_from_usage(db, run_id, self.owner)
            await settle_run_budget_from_usage(db, run_id, self.owner)
            await db.commit()
            self.assertEqual(settled.confirmed_cost, Decimal("2"))
            account = await db.get(GatewayBudgetAccount, account_id)
            self.assertEqual((account.spent_amount, account.reserved_amount),
                             (Decimal("2"), Decimal("0")))
            self.assertEqual(len((await db.scalars(select(GatewayBudgetAdjustment))).all()), 1)
        async with self.sessions() as db:
            await record_usage_receipt(db, call.id, "correction", "succeeded",
                                       TokenUsage(3_000_000, 0, 0), "provider")
            await db.commit()
        async with self.sessions() as db:
            corrected = await settle_run_budget_from_usage(db, run_id, self.owner)
            await settle_run_budget_from_usage(db, run_id, self.owner)
            await db.commit()
            self.assertEqual(corrected.confirmed_cost, Decimal("3"))
            account = await db.get(GatewayBudgetAccount, account_id)
            self.assertEqual((account.spent_amount, account.reserved_amount),
                             (Decimal("3"), Decimal("0")))
            adjustments = (await db.scalars(select(GatewayBudgetAdjustment).order_by(
                GatewayBudgetAdjustment.created_at))).all()
            self.assertEqual(sorted(row.delta for row in adjustments),
                             [Decimal("1"), Decimal("2")])
            with self.assertRaisesRegex(ValueError, "不可再发起"):
                await start_model_call(db, run_id, self.owner, "test", "budget")
        async with self.sessions() as db:
            await record_usage_receipt(db, call.id, "lower-correction", "succeeded",
                                       TokenUsage(1_000_000, 0, 0), "provider")
            await settle_run_budget_from_usage(db, run_id, self.owner)
            await db.commit()
            account = await db.get(GatewayBudgetAccount, account_id)
            self.assertEqual(account.spent_amount, Decimal("1"))
            adjustments = (await db.scalars(select(GatewayBudgetAdjustment))).all()
            self.assertEqual(sorted(row.delta for row in adjustments),
                             [Decimal("-2"), Decimal("1"), Decimal("2")])

    async def test_unknown_cost_keeps_reservation_until_late_receipt(self):
        account_id, run_id = await self.add_account(), await self.add_run()
        async with self.sessions() as db:
            db.add(GatewayPriceVersion(
                id="late-usd", provider="test", model="late", currency="USD",
                input_per_million=Decimal("1"), output_per_million=Decimal("0"),
                cached_input_per_million=Decimal("0"),
                effective_at=datetime.now(UTC),
            ))
            await db.flush()
            await reserve_run_budget(db, run_id, self.owner, "USD", self.today, Decimal("7"))
            call = await start_model_call(db, run_id, self.owner, "test", "late")
            await transition_run(db, run_id, RunStatus.CREATED, RunStatus.RUNNING)
            await record_usage_receipt(db, call.id, "missing", "cancelled",
                                       TokenUsage(None, None, None), "provider")
            await transition_run(db, run_id, RunStatus.RUNNING, RunStatus.CANCELLED)
            await db.commit()
        async with self.sessions() as db:
            reservation = await settle_run_budget_from_usage(db, run_id, self.owner)
            self.assertEqual(reservation.status, "reserved")
            self.assertEqual((await db.get(GatewayBudgetAccount, account_id)).reserved_amount,
                             Decimal("7"))
            self.assertEqual(len((await db.scalars(select(GatewayBudgetAdjustment))).all()), 0)
        async with self.sessions() as db:
            await record_usage_receipt(db, call.id, "late", "cancelled",
                                       TokenUsage(4_000_000, 0, 0), "provider")
            await db.commit()
        async with self.sessions() as db:
            reservation = await settle_run_budget_from_usage(db, run_id, self.owner)
            await db.commit()
            self.assertEqual(reservation.confirmed_cost, Decimal("4"))
            account = await db.get(GatewayBudgetAccount, account_id)
            self.assertEqual((account.spent_amount, account.reserved_amount),
                             (Decimal("4"), Decimal("0")))

    async def test_mismatched_currency_fails_closed(self):
        account_id, run_id = await self.add_account(), await self.add_run()
        async with self.sessions() as db:
            db.add(GatewayPriceVersion(
                id="budget-eur", provider="test", model="foreign", currency="EUR",
                input_per_million=Decimal("1"), output_per_million=Decimal("0"),
                cached_input_per_million=Decimal("0"),
                effective_at=datetime.now(UTC),
            ))
            await db.flush()
            await reserve_run_budget(db, run_id, self.owner, "USD", self.today, Decimal("7"))
            call = await start_model_call(db, run_id, self.owner, "test", "foreign")
            await transition_run(db, run_id, RunStatus.CREATED, RunStatus.RUNNING)
            await record_usage_receipt(db, call.id, "eur", "succeeded",
                                       TokenUsage(1_000_000, 0, 0), "provider")
            await transition_run(db, run_id, RunStatus.RUNNING, RunStatus.SUCCEEDED)
            await db.commit()
        async with self.sessions() as db:
            with self.assertRaisesRegex(ValueError, "币种"):
                await settle_run_budget_from_usage(db, run_id, self.owner)
            await db.rollback()
            account = await db.get(GatewayBudgetAccount, account_id)
            self.assertEqual((account.spent_amount, account.reserved_amount),
                             (Decimal("0"), Decimal("7")))
