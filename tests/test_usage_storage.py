"""模型调用与回执存储的事务、定价和迟到回执测试。"""

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.base import Base
from api.models import (
    AgentRun, ChatMessage, ChatSession, GatewayCallReconciliation, GatewayModelCall,
    GatewayPriceVersion, GatewayUsageEvent, User,
)
from api.run_storage import new_run
from api.usage_storage import (
    finish_model_call, get_run_usage_summary, list_stale_model_calls,
    reconcile_stale_model_calls,
    record_usage_receipt, start_model_call,
)
from src.agent.run_policy import RunLimits
from src.agent.usage_pricing import TokenUsage


class UsageStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.owner, session_id, request_id, message_id = (uuid.uuid4() for _ in range(4))
        async with self.sessions() as db:
            db.add(User(id=self.owner, username=f"usage-{self.owner}", hashed_password="test"))
            await db.flush()
            db.add(ChatSession(id=session_id, owner_id=self.owner,
                               title="调用测试", checkpoint_thread_id=str(session_id)))
            await db.flush()
            db.add(ChatMessage(id=message_id, session_id=session_id,
                               request_id=request_id, seq=1, role="assistant",
                               status="streaming", content=""))
            await db.flush()
            run = new_run(self.owner, session_id, request_id, message_id, RunLimits())
            db.add(run)
            await db.commit()
            self.run_id = run.id
            self.run_deadline = run.deadline_at

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_price_at_call_start_and_late_receipt(self):
        started = datetime.now(UTC)
        async with self.sessions() as db:
            for version, at, rate in (
                ("early", started - timedelta(days=1), "2.000000"),
                ("future", started + timedelta(days=1), "20.000000"),
            ):
                db.add(GatewayPriceVersion(
                    id=version, provider="openai", model="test", currency="USD",
                    input_per_million=Decimal(rate), output_per_million=Decimal("10"),
                    cached_input_per_million=Decimal("0.25"), effective_at=at,
                ))
            call = await start_model_call(db, self.run_id, self.owner, "openai", "test", now=started)
            await db.commit()

        async with self.sessions() as db:
            db.add(GatewayPriceVersion(
                id="backdated", provider="openai", model="test", currency="USD",
                input_per_million=Decimal("99"), output_per_million=Decimal("99"),
                cached_input_per_million=Decimal("99"),
                effective_at=started - timedelta(hours=1),
            ))
            await db.flush()
            self.assertTrue(await finish_model_call(db, call.id, "cancelled"))
            self.assertFalse(await finish_model_call(db, call.id, "succeeded"))
            receipt = await record_usage_receipt(
                db, call.id, "late-provider-receipt", "cancelled",
                TokenUsage(1_000_000, 200_000, 400_000), "provider",
            )
            await db.commit()
            self.assertEqual(receipt.price_version_id, "early")
            self.assertEqual(receipt.cost, Decimal("3.300000"))
            self.assertEqual(receipt.currency, "USD")

    async def test_unknown_price_and_missing_tokens_leave_cost_null(self):
        async with self.sessions() as db:
            call = await start_model_call(db, self.run_id, self.owner, "openai", "unpriced")
            await db.commit()
        async with self.sessions() as db:
            receipt = await record_usage_receipt(
                db, call.id, "missing", "unknown", TokenUsage(None, None, None), "provider",
            )
            await db.commit()
            self.assertIsNone(receipt.cost)
            self.assertIsNone(receipt.price_version_id)
            self.assertIsNone(receipt.input_tokens)

    async def test_same_receipt_is_idempotent_and_conflict_preserves_transaction(self):
        async with self.sessions() as db:
            call = await start_model_call(db, self.run_id, self.owner, "openai", "test")
            await db.commit()
        async with self.sessions() as db:
            original = await record_usage_receipt(
                db, call.id, "receipt", "succeeded", TokenUsage(1, 2, 0), "provider",
            )
            duplicate = await record_usage_receipt(
                db, call.id, "receipt", "succeeded", TokenUsage(1, 2, 0), "provider",
            )
            self.assertEqual(original.id, duplicate.id)
            with self.assertRaisesRegex(ValueError, "不同用量"):
                await record_usage_receipt(
                    db, call.id, "receipt", "succeeded", TokenUsage(9, 2, 0), "provider",
                )
            await db.commit()
            events = (await db.scalars(select(GatewayUsageEvent).where(
                GatewayUsageEvent.call_id == call.id))).all()
            self.assertEqual(len(events), 1)

    async def test_run_total_selects_latest_confirmed_receipt_per_call(self):
        started = datetime.now(UTC)
        async with self.sessions() as db:
            db.add(GatewayPriceVersion(
                id="summary-price", provider="openai", model="test", currency="USD",
                input_per_million=Decimal("1"), output_per_million=Decimal("2"),
                cached_input_per_million=Decimal("0"),
                effective_at=started - timedelta(days=1),
            ))
            await db.flush()
            billed = await start_model_call(db, self.run_id, self.owner, "openai", "test",
                                            now=started)
            unknown = await start_model_call(db, self.run_id, self.owner, "openai", "test",
                                             now=started)
            await db.commit()
        async with self.sessions() as db:
            await finish_model_call(db, billed.id, "failed")
            await record_usage_receipt(
                db, billed.id, "first", "failed", TokenUsage(1_000_000, 0, 0),
                "provider", now=started + timedelta(seconds=1),
            )
            await record_usage_receipt(
                db, billed.id, "correction", "failed", TokenUsage(2_000_000, 0, 0),
                "provider", now=started + timedelta(seconds=2),
            )
            await record_usage_receipt(
                db, unknown.id, "missing", "unknown", TokenUsage(None, None, None),
                "provider", now=started + timedelta(seconds=3),
            )
            await db.commit()
            summary = await get_run_usage_summary(db, self.run_id, self.owner)
            self.assertEqual(summary.call_count, 2)
            self.assertEqual(summary.priced_call_count, 1)
            self.assertEqual(summary.unknown_cost_call_count, 1)
            self.assertEqual(summary.costs_by_currency, {"USD": Decimal("2.000000")})
            self.assertEqual((await get_run_usage_summary(
                db, self.run_id, uuid.uuid4())).call_count, 0)

    async def test_stale_call_requires_deadline_and_never_estimates_cost(self):
        old_start = self.run_deadline - timedelta(minutes=10)
        async with self.sessions() as db:
            call = await start_model_call(db, self.run_id, self.owner,
                                          "openai", "test", now=old_start)
            await db.commit()
        async with self.sessions() as db:
            self.assertEqual(await reconcile_stale_model_calls(
                db, self.owner, "test-evidence", now=self.run_deadline + timedelta(seconds=299)), [])
            self.assertEqual(await list_stale_model_calls(
                db, now=self.run_deadline + timedelta(seconds=300)), [call.id])
            self.assertEqual(await reconcile_stale_model_calls(
                db, self.owner, "test-evidence", now=self.run_deadline + timedelta(seconds=300)),
                [call.id])
            await db.commit()
            self.assertEqual((await db.get(GatewayModelCall, call.id)).status, "unknown")
            self.assertTrue((await db.get(AgentRun, self.run_id)).recovery_required)
            recovery = await db.scalar(select(GatewayUsageEvent).where(
                GatewayUsageEvent.call_id == call.id))
            self.assertEqual(recovery.event_key, "recovery")
            self.assertIsNone(recovery.cost)
            audit = await db.get(GatewayCallReconciliation, call.id)
            self.assertEqual(audit.reviewer_id, self.owner)
            self.assertEqual(audit.evidence_ref, "test-evidence")


if __name__ == "__main__":
    unittest.main()
