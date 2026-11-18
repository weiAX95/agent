"""M3 账本表的离线约束测试；PostgreSQL 迁移另行验收。"""

import unittest
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.base import Base
from api.models import (
    AgentRun, ChatMessage, ChatSession, GatewayModelCall,
    GatewayPriceVersion, GatewayUsageEvent, User,
)
from src.agent.run_policy import RunLimits
from api.run_storage import new_run


class UsageSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.owner, self.session_id, self.message_id, self.request_id = (
            uuid.uuid4() for _ in range(4)
        )
        async with self.sessions() as db:
            db.add(User(id=self.owner, username=f"usage-{self.owner}", hashed_password="test"))
            await db.flush()
            db.add(ChatSession(id=self.session_id, owner_id=self.owner,
                               title="账本测试", checkpoint_thread_id=str(self.session_id)))
            await db.flush()
            db.add(ChatMessage(id=self.message_id, session_id=self.session_id,
                               request_id=self.request_id, seq=1, role="assistant",
                               status="streaming", content=""))
            await db.flush()
            self.run = new_run(self.owner, self.session_id, self.request_id,
                               self.message_id, RunLimits())
            db.add(self.run)
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_unknown_usage_remains_null_and_receipt_is_idempotent(self):
        call_id = uuid.uuid4()
        async with self.sessions() as db:
            db.add(GatewayModelCall(id=call_id, run_id=self.run.id,
                                    owner_id=self.owner, provider="openai", model="test"))
            await db.flush()
            db.add(GatewayUsageEvent(id=uuid.uuid4(), call_id=call_id,
                                     event_key="first", status="unknown", source="provider"))
            await db.commit()
            stored = await db.scalar(select(GatewayUsageEvent).where(
                GatewayUsageEvent.call_id == call_id))
            self.assertIsNone(stored.input_tokens)
            self.assertIsNone(stored.cost)
            db.add(GatewayUsageEvent(id=uuid.uuid4(), call_id=call_id,
                                     event_key="first", status="succeeded", source="provider"))
            with self.assertRaises(IntegrityError):
                await db.commit()
            await db.rollback()

    async def test_price_version_and_decimal_cost_round_trip(self):
        call_id = uuid.uuid4()
        async with self.sessions() as db:
            db.add(GatewayPriceVersion(
                id="price-v1", provider="openai", model="test", currency="USD",
                input_per_million=Decimal("2.500000"),
                output_per_million=Decimal("10.000000"),
                cached_input_per_million=Decimal("0.250000"),
                effective_at=datetime.now(UTC),
            ))
            db.add(GatewayModelCall(id=call_id, run_id=self.run.id,
                                    owner_id=self.owner, provider="openai", model="test"))
            await db.flush()
            db.add(GatewayUsageEvent(
                id=uuid.uuid4(), call_id=call_id, event_key="confirmed",
                status="succeeded", input_tokens=1_000_000,
                output_tokens=200_000, cached_input_tokens=400_000,
                price_version_id="price-v1", currency="USD",
                cost=Decimal("3.600000000000"), source="provider",
            ))
            await db.commit()
            stored = await db.scalar(select(GatewayUsageEvent).where(
                GatewayUsageEvent.call_id == call_id))
            self.assertEqual(stored.cost, Decimal("3.600000000000"))
            self.assertEqual(stored.price_version_id, "price-v1")


if __name__ == "__main__":
    unittest.main()
