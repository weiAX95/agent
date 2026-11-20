"""M3 正式迁移与精确账本字段在一次性 PostgreSQL 中的验证。"""

import os
import unittest
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.models import (
    AgentRun, ChatMessage, ChatSession, GatewayModelCall,
    GatewayPriceVersion, GatewayUsageEvent, User,
)
from api.run_storage import new_run
from api.usage_storage import finish_model_call, record_usage_receipt, start_model_call
from src.agent.run_policy import RunLimits
from src.agent.usage_pricing import TokenUsage


DB_URL = os.getenv("USAGE_TEST_POSTGRES_URL")


@unittest.skipUnless(
    DB_URL and os.getenv("USAGE_TEST_POSTGRES_ALLOW_REBUILD") == "1",
    "仅在明确授权的一次性 gateway_m3_test 数据库运行",
)
class UsagePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_migration_and_receipt_constraints(self):
        if make_url(DB_URL).database != "gateway_m3_test":
            raise ValueError("M3 PostgreSQL 测试仅允许 gateway_m3_test 数据库")
        engine = create_async_engine(DB_URL)
        try:
            async with engine.begin() as connection:
                # 前置 M2 表只用于隔离测试；目标库必须先执行正式 M2 迁移。
                for table in (User.__table__, ChatSession.__table__, ChatMessage.__table__,
                              AgentRun.__table__):
                    await connection.run_sync(table.create, checkfirst=True)
                migration = Path("migrations/20261118_gateway_usage.sql").read_text()
                statements = [part.strip() for part in migration.split(";")
                              if "CREATE TABLE" in part or "CREATE INDEX" in part]
                for _ in range(2):
                    for statement in statements:
                        await connection.exec_driver_sql(statement)
                    await connection.exec_driver_sql(Path(
                        "migrations/20261120_gateway_call_price_snapshot.sql").read_text())
                    append_only_sql = Path(
                        "migrations/20261120_gateway_ledger_append_only.sql").read_text()
                    for statement in append_only_sql.split("\n\n"):
                        if "CREATE OR REPLACE" in statement:
                            await connection.exec_driver_sql(statement)
                for name in ("gateway_price_versions", "gateway_model_calls", "gateway_usage_events"):
                    self.assertIsNotNone(await connection.scalar(text(
                        "SELECT to_regclass(:name)"), {"name": f"public.{name}"}))
                self.assertEqual(await connection.scalar(text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name = "
                    "'gateway_model_calls' AND column_name = 'price_version_id'")), 1)

            sessions = async_sessionmaker(engine, expire_on_commit=False)
            owner, session_id, request_id, message_id, call_id = (uuid.uuid4() for _ in range(5))
            async with sessions() as db:
                db.add(User(id=owner, username=f"usage-{owner}", hashed_password="test"))
                await db.flush()
                db.add(ChatSession(id=session_id, owner_id=owner,
                                   title="M3 测试", checkpoint_thread_id=str(session_id)))
                await db.flush()
                db.add(ChatMessage(id=message_id, session_id=session_id,
                                   request_id=request_id, seq=1, role="assistant",
                                   status="streaming", content=""))
                await db.flush()
                run = new_run(owner, session_id, request_id, message_id, RunLimits())
                run_id = run.id
                db.add(run)
                await db.flush()
                db.add(GatewayPriceVersion(
                    id="test-price-v1", provider="openai", model="test", currency="USD",
                    input_per_million=Decimal("2.500000"),
                    output_per_million=Decimal("10.000000"),
                    cached_input_per_million=Decimal("0.250000"),
                    effective_at=datetime.now(UTC),
                ))
                db.add(GatewayModelCall(id=call_id, run_id=run.id,
                                        owner_id=owner, provider="openai", model="test"))
                await db.flush()
                db.add(GatewayUsageEvent(id=uuid.uuid4(), call_id=call_id,
                                         event_key="unknown", status="unknown", source="provider"))
                db.add(GatewayUsageEvent(
                    id=uuid.uuid4(), call_id=call_id, event_key="receipt-1",
                    status="succeeded", input_tokens=1_000_000,
                    output_tokens=200_000, cached_input_tokens=400_000,
                    price_version_id="test-price-v1", currency="USD",
                    cost=Decimal("3.600000000000"), source="provider",
                ))
                await db.commit()
                events = (await db.scalars(select(GatewayUsageEvent).where(
                    GatewayUsageEvent.call_id == call_id))).all()
                self.assertEqual(len(events), 2)
                self.assertIsNone(next(e for e in events if e.event_key == "unknown").cost)
                self.assertEqual(next(e for e in events if e.event_key == "receipt-1").cost,
                                 Decimal("3.600000000000"))
                db.add(GatewayUsageEvent(id=uuid.uuid4(), call_id=call_id,
                                         event_key="receipt-1", status="unknown", source="provider"))
                with self.assertRaises(IntegrityError):
                    await db.commit()
                await db.rollback()
                # 调用先落库再发送请求；完成后仍可按原始价格接收迟到回执。
                later_call = await start_model_call(
                    db, run_id, owner, "openai", "test",
                )
                await db.commit()
                self.assertTrue(await finish_model_call(db, later_call.id, "cancelled"))
                late = await record_usage_receipt(
                    db, later_call.id, "late", "cancelled",
                    TokenUsage(1_000_000, 200_000, 400_000), "provider",
                )
                repeated = await record_usage_receipt(
                    db, later_call.id, "late", "cancelled",
                    TokenUsage(1_000_000, 200_000, 400_000), "provider",
                )
                await db.commit()
                self.assertEqual(late.id, repeated.id)
                self.assertEqual(late.cost, Decimal("3.600000000000"))
            for mutation in (
                "UPDATE gateway_price_versions SET input_per_million = 999 WHERE id = 'test-price-v1'",
                "DELETE FROM gateway_price_versions WHERE id = 'test-price-v1'",
                "UPDATE gateway_usage_events SET cost = 999 WHERE event_key = 'late'",
                "DELETE FROM gateway_usage_events WHERE event_key = 'late'",
            ):
                with self.subTest(mutation=mutation), self.assertRaises(DBAPIError):
                    async with engine.begin() as connection:
                        await connection.execute(text(mutation))
            async with engine.connect() as connection:
                self.assertEqual(await connection.scalar(text(
                    "SELECT input_per_million FROM gateway_price_versions "
                    "WHERE id = 'test-price-v1'")), Decimal("2.500000"))
                self.assertEqual(await connection.scalar(text(
                    "SELECT count(*) FROM gateway_usage_events WHERE event_key = 'late'")), 1)
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
