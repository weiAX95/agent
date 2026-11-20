"""模型调用与回执存储的事务、定价和迟到回执测试。"""

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.base import Base
from api.models import AgentRun, ChatMessage, ChatSession, GatewayPriceVersion, GatewayUsageEvent, User
from api.run_storage import new_run
from api.usage_storage import finish_model_call, record_usage_receipt, start_model_call
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


if __name__ == "__main__":
    unittest.main()
