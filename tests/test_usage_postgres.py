"""M3 正式迁移与精确账本字段在一次性 PostgreSQL 中的验证。"""

import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.models import (
    AgentRun, ChatMessage, ChatSession, GatewayModelCall,
    GatewayPriceVersion, GatewayUsageEvent, User,
)
from api.run_storage import new_run
from api.usage_storage import (
    finish_model_call, get_run_usage_summary, reconcile_stale_model_calls,
    record_usage_receipt, start_model_call,
)
from api.usage_attempt_sink import DatabaseAttemptSink
from src.agent.physical_transport import (
    AsyncMeteredClient, MeteredClient, MeteredRun, active_metered_run,
)
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
                run_deadline = run.deadline_at
                db.add(run)
                await db.flush()
                db.add(GatewayPriceVersion(
                    id="test-price-v1", provider="openai", model="test", currency="USD",
                    input_per_million=Decimal("2.500000"),
                    output_per_million=Decimal("10.000000"),
                    cached_input_per_million=Decimal("0.250000"),
                    effective_at=datetime.now(UTC) - timedelta(days=1),
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
                summary = await get_run_usage_summary(db, run_id, owner)
                self.assertEqual(summary.call_count, 2)
                self.assertEqual(summary.priced_call_count, 2)
                self.assertEqual(summary.costs_by_currency,
                                 {"USD": Decimal("7.200000000000")})
            sync_engine = create_engine(make_url(DB_URL).set(drivername="postgresql+psycopg"))
            try:
                sink = DatabaseAttemptSink(sync_engine, sessions)
                run_context = MeteredRun(run_id, owner, "test-trace")
                sync_call = sink.start(run_context, "openai", "test")
                sink.finish(sync_call, "failed")
                async_call = await sink.astart(run_context, "openai", "test")
                await sink.afinish(async_call, "succeeded")
                await sink.arecord_usage(async_call, "succeeded",
                                         TokenUsage(1_000_000, 200_000, 400_000))
                token = active_metered_run.set(run_context)
                try:
                    with MeteredClient(sink, "openai", "test", transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, stream=httpx.ByteStream(
                            b'{"usage":{"prompt_tokens":1000000,"completion_tokens":200000,'
                            b'"prompt_tokens_details":{"cached_tokens":400000}}}'
                        )),
                    )) as client:
                        self.assertEqual(client.post(
                            "https://example.test/v1/chat/completions").status_code, 200)
                    async with AsyncMeteredClient(
                        sink, "openai", "test", transport=httpx.MockTransport(
                            lambda request: httpx.Response(503, stream=httpx.ByteStream(b"{}")),
                        ),
                    ) as client:
                        self.assertEqual((await client.post(
                            "https://example.test/v1/responses")).status_code, 503)
                finally:
                    active_metered_run.reset(token)
                async with sessions() as db:
                    for observed_call_id, status in ((sync_call, "failed"),
                                                     (async_call, "succeeded")):
                        call = await db.get(GatewayModelCall, observed_call_id)
                        self.assertEqual(call.status, status)
                        self.assertEqual(call.trace_id, "test-trace")
                        receipt = await db.scalar(select(GatewayUsageEvent).where(
                            GatewayUsageEvent.call_id == observed_call_id))
                        self.assertEqual(receipt.event_key, "transport")
                        self.assertIsNone(receipt.cost)
                    recent = (await db.scalars(select(GatewayModelCall).where(
                        GatewayModelCall.run_id == run_id,
                        GatewayModelCall.trace_id == "test-trace",
                    ))).all()
                    self.assertEqual(sorted(call.status for call in recent),
                                     ["failed", "failed", "succeeded", "succeeded"])
                    confirmed = (await db.scalars(select(GatewayUsageEvent).where(
                        GatewayUsageEvent.call_id.in_([call.id for call in recent]),
                        GatewayUsageEvent.source == "provider",
                    ))).all()
                    self.assertEqual(len(confirmed), 2)
                    self.assertTrue(all(event.cost == Decimal("3.600000000000")
                                        for event in confirmed))
                async with sessions() as db:
                    stale = await start_model_call(
                        db, run_id, owner, "openai", "test",
                        now=run_deadline - timedelta(minutes=3),
                    )
                    await db.commit()
                async with sessions() as db:
                    recovered = await reconcile_stale_model_calls(
                        db, now=run_deadline + timedelta(minutes=6),
                    )
                    self.assertIn(stale.id, recovered)
                    self.assertIn(call_id, recovered)
                    await db.commit()
                    self.assertEqual(await reconcile_stale_model_calls(
                        db, now=run_deadline + timedelta(minutes=6)), [])
                    run_row = await db.get(AgentRun, run_id)
                    self.assertTrue(run_row.recovery_required)
                await sink.afinish(stale.id, "succeeded")
                sink.finish(call_id, "succeeded")
                await sink.arecord_usage(stale.id, "succeeded",
                                         TokenUsage(1_000_000, 200_000, 400_000))
                async with sessions() as db:
                    self.assertEqual((await db.get(GatewayModelCall, stale.id)).status, "unknown")
                    evidence = (await db.scalars(select(GatewayUsageEvent).where(
                        GatewayUsageEvent.call_id == stale.id))).all()
                    self.assertEqual({event.event_key for event in evidence},
                                     {"recovery", "transport-late", "provider"})
                    self.assertEqual(next(event for event in evidence if event.event_key ==
                                          "provider").cost, Decimal("3.600000000000"))
                    self.assertIsNotNone(await db.scalar(select(GatewayUsageEvent).where(
                        GatewayUsageEvent.call_id == call_id,
                        GatewayUsageEvent.event_key == "transport-late",
                    )))
            finally:
                sync_engine.dispose()
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
