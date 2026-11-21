"""Gateway request inspection covers ownership, retries and unknown billing."""

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.auth import get_current_user
from api.base import Base
from api.db import get_db
from api.models import (
    ChatMessage, ChatRunEvent, ChatSession, GatewayModelCall, GatewayPriceVersion,
    GatewayUsageEvent, User,
)
from api.routers.run_diagnostics import recent_router, router
from api.run_storage import new_run
from src.agent.run_policy import RunLimits


class RunDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.owner, self.other, self.session_id, self.request_id, self.message_id = (
            uuid.uuid4() for _ in range(5)
        )
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.include_router(recent_router)

        async def test_db():
            async with self.sessions() as db:
                yield db

        self.app.dependency_overrides[get_db] = test_db
        self.app.dependency_overrides[get_current_user] = lambda: {"user_id": str(self.owner)}
        now = datetime.now(UTC)
        async with self.sessions() as db:
            db.add_all([
                User(id=self.owner, username="diagnostics-owner", hashed_password="test"),
                User(id=self.other, username="diagnostics-other", hashed_password="test"),
            ])
            await db.flush()
            db.add(ChatSession(id=self.session_id, owner_id=self.owner, title="诊断测试",
                               checkpoint_thread_id=str(self.session_id)))
            await db.flush()
            db.add(ChatMessage(
                id=self.message_id, session_id=self.session_id, request_id=self.request_id,
                seq=1, role="assistant", status="streaming",
                content="private prompt and answer must not leak",
            ))
            await db.flush()
            run = new_run(self.owner, self.session_id, self.request_id,
                          self.message_id, RunLimits(), now=now)
            self.run_id = run.id
            db.add(run)
            db.add(ChatRunEvent(
                id=uuid.uuid4(), message_id=self.message_id, sequence=1,
                stage="research", status="started", summary="开始查找资料", sources=[],
            ))
            db.add(GatewayPriceVersion(
                id="diag-price", provider="test", model="model", currency="USD",
                input_per_million=Decimal("2"), output_per_million=Decimal("10"),
                cached_input_per_million=Decimal("0"), effective_at=now - timedelta(days=1),
            ))
            await db.flush()
            self.failed_id, self.succeeded_id = uuid.uuid4(), uuid.uuid4()
            self.corrected_event_id = uuid.uuid4()
            db.add_all([
                GatewayModelCall(
                    id=self.failed_id, run_id=run.id, owner_id=self.owner,
                    provider="test", model="model", status="failed", trace_id="trace-one",
                    started_at=now, finished_at=now + timedelta(milliseconds=50),
                    price_version_id="diag-price",
                ),
                GatewayModelCall(
                    id=self.succeeded_id, run_id=run.id, owner_id=self.owner,
                    provider="test", model="model", status="succeeded", trace_id="trace-one",
                    started_at=now + timedelta(milliseconds=60),
                    finished_at=now + timedelta(milliseconds=200),
                    price_version_id="diag-price",
                ),
            ])
            await db.flush()
            db.add_all([
                GatewayUsageEvent(
                    id=uuid.uuid4(), call_id=self.failed_id, event_key="transport",
                    status="failed", source="transport", created_at=now,
                ),
                GatewayUsageEvent(
                    id=uuid.uuid4(), call_id=self.succeeded_id, event_key="provider",
                    status="succeeded", source="provider", input_tokens=1_000_000,
                    output_tokens=0, cached_input_tokens=0, price_version_id="diag-price",
                    currency="USD", cost=Decimal("2.000000000000"),
                    created_at=now + timedelta(milliseconds=200),
                ),
                GatewayUsageEvent(
                    id=self.corrected_event_id, call_id=self.succeeded_id,
                    event_key="provider-correction", status="succeeded", source="provider",
                    input_tokens=1_000_000, output_tokens=100_000,
                    cached_input_tokens=0, price_version_id="diag-price",
                    currency="USD", cost=Decimal("3.000000000000"),
                    created_at=now + timedelta(milliseconds=210),
                ),
            ])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def get_diagnostics(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        ) as client:
            return await client.get(
                f"/api/sessions/{self.session_id}/requests/{self.request_id}/diagnostics"
            )

    async def test_owner_sees_each_attempt_and_unknown_cost(self):
        response = await self.get_diagnostics()
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["run"]["run_id"], str(self.run_id))
        self.assertEqual(data["steps"][0]["stage"], "research")
        self.assertEqual([call["status"] for call in data["calls"]], ["failed", "succeeded"])
        self.assertEqual(data["calls"][0]["duration_ms"], 50)
        self.assertIsNone(data["calls"][0]["usage_events"][0]["cost"])
        self.assertIsNone(data["calls"][0]["selected_cost_event_id"])
        self.assertEqual(data["calls"][1]["selected_cost_event_id"],
                         str(self.corrected_event_id))
        self.assertEqual(data["summary"]["unknown_cost_call_count"], 1)
        self.assertEqual(Decimal(data["summary"]["costs_by_currency"]["USD"]), Decimal("3"))
        self.assertNotIn("private prompt", response.text)

    async def test_other_user_and_missing_request_are_hidden(self):
        self.app.dependency_overrides[get_current_user] = lambda: {"user_id": str(self.other)}
        self.assertEqual((await self.get_diagnostics()).status_code, 404)
        self.app.dependency_overrides[get_current_user] = lambda: {"user_id": str(self.owner)}
        self.request_id = uuid.uuid4()
        self.assertEqual((await self.get_diagnostics()).status_code, 404)

    async def test_missing_bearer_is_rejected(self):
        self.app.dependency_overrides.pop(get_current_user)
        self.assertEqual((await self.get_diagnostics()).status_code, 401)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        ) as client:
            self.assertEqual((await client.get("/api/runs/recent")).status_code, 401)

    async def test_recent_runs_list_only_owned_visible_sessions(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        ) as client:
            response = await client.get("/api/runs/recent")
            self.assertEqual(response.status_code, 200)
            items = response.json()["data"]["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["request_id"], str(self.request_id))
            self.app.dependency_overrides[get_current_user] = lambda: {"user_id": str(self.other)}
            self.assertEqual((await client.get("/api/runs/recent")).json()["data"]["items"], [])
            self.assertEqual((await client.get("/api/runs/recent?limit=0")).status_code, 422)

    async def test_deleted_session_is_hidden(self):
        async with self.sessions() as db:
            session = await db.get(ChatSession, self.session_id)
            session.deleted_at = datetime.now(UTC)
            await db.commit()
        self.assertEqual((await self.get_diagnostics()).status_code, 404)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        ) as client:
            self.assertEqual((await client.get("/api/runs/recent")).json()["data"]["items"], [])
