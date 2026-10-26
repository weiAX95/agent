"""Explicit local integration check. Creates/drops only a random isolated schema.

Run from repository root: .venv/bin/python -m scripts.verify.chat_storage_postgres
Uses DATABASE_URL from the environment or .env. Never calls external model services.
"""

import asyncio
import json
import os
import sys
import uuid
from types import ModuleType
from unittest.mock import patch
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.base import Base
from api.chat_storage import create_session, session_execution_lock
from api.models import ChatLog, ChatMessage, User
from src.agent.checkpoint import make_thread_id

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from chat_api_fixture import make_app


async def verify(url, schema):
    pg_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    engine = create_async_engine(
        url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False),
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    pool_options = dict(
        conninfo=pg_url, min_size=1, max_size=5, open=False,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0,
                "options": f"-c search_path={schema}"},
    )
    pool = AsyncConnectionPool(**pool_options)
    lock_pool = AsyncConnectionPool(**pool_options)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await pool.open(wait=True)
        await lock_pool.open(wait=True)
        owner, other = uuid.uuid4(), uuid.uuid4()
        async with factory() as db:
            db.add_all([User(id=owner, username="integration-a", hashed_password="test-only"),
                        User(id=other, username="integration-b", hashed_password="test-only")])
            await db.commit()
            session = await create_session(db, owner, "集成测试")
            sid = session.id
            second = await create_session(db, owner, "另一个会话")
            sid2 = second.id

        async with session_execution_lock(lock_pool, owner, sid):
            async with factory() as db:
                await db.commit()
            try:
                async with session_execution_lock(lock_pool, owner, sid):
                    raise AssertionError("Same-session lock did not exclude a second connection")
            except HTTPException as exc:
                assert exc.status_code == 409
            async with session_execution_lock(lock_pool, owner, sid2):
                pass
        async with session_execution_lock(lock_pool, owner, sid):
            pass
        print("PASS real PostgreSQL lock spans commits; same session excluded, another session allowed; released")

        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        agent = create_agent(model=FakeListChatModel(responses=["第一轮回答", "第二轮回答"]), tools=[], checkpointer=saver)
        calls = []

        async def model(**kwargs):
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": kwargs["message"]}]},
                config={"configurable": {"thread_id": make_thread_id(kwargs["user_id"], kwargs["session_id"])}}
            )
            calls.append(result)
            answer = result["messages"][-1].content
            await kwargs["on_token"](answer)
            return "done", answer

        app = make_app(factory, lock_pool, model)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = {"x-test-user": str(owner)}
            request = {"session_id": str(sid), "request_id": str(uuid.uuid4()), "message": "第一轮问题"}
            response = await client.post("/api/chat", headers=headers, json=request)
            assert response.status_code == 200 and "event: done" in response.text
            replay = await client.post("/api/chat", headers=headers, json=request)
            assert '"replayed": true' in replay.text and len(calls) == 1
            conflict = await client.post("/api/chat", headers=headers, json={**request, "message": "改了正文"})
            assert conflict.status_code == 409
            forbidden = await client.post("/api/chat", headers={"x-test-user": str(other)}, json=request)
            assert forbidden.status_code == 404
            second_request = {**request, "request_id": str(uuid.uuid4()), "message": "第二轮问题"}
            response = await client.post("/api/chat", headers=headers, json=second_request)
            assert response.status_code == 200 and "event: done" in response.text
            assert len(calls[-1]["messages"]) == 4
        async with factory() as db:
            rows = (await db.scalars(select(ChatMessage).order_by(ChatMessage.seq))).all()
            logs = (await db.scalars(select(ChatLog))).all()
            assert len(rows) == 4 and len(logs) == 2
            assert [row.seq for row in rows] == [1, 2, 3, 4]
            assert all(row.status == "completed" for row in rows)
        print("PASS real ORM/HTTP two-turn persistence, actual PostgreSQL checkpoint history, replay, conflict and isolation")

        # Verify the actual registered app and JWT dependency as well. Replace only
        # the production model module so imports cannot load Milvus or call a provider.
        stub = ModuleType("src.agent.main")
        stub.build_agent = lambda _saver: agent
        stub.chat_stream = model
        with patch.dict(sys.modules, {"src.agent.main": stub}):
            from api import server
        from api.security import create_access_token

        async def isolated_db():
            async with factory() as db:
                yield db

        server.app.state.agent = agent
        server.app.state.chat_lock_pool = lock_pool
        server.app.dependency_overrides[server.get_db] = isolated_db
        try:
            async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as client:
                token = create_access_token(str(owner))
                other_token = create_access_token(str(other))
                headers = {"Authorization": "Bearer " + token}
                assert (await client.post("/api/sessions", json={})).status_code == 401
                created = await client.post("/api/sessions", headers=headers, json={"title": "真实路由"})
                assert created.status_code == 201
                actual_request = {"session_id": created.json()["data"]["id"], "request_id": str(uuid.uuid4()), "message": "实际接口"}
                forbidden = await client.post("/api/chat", headers={"Authorization": "Bearer " + other_token}, json=actual_request)
                assert forbidden.status_code == 404
                response = await client.post("/api/chat", headers=headers, json=actual_request)
                assert response.status_code == 200 and "event: done" in response.text
                frame = next(frame for frame in response.text.split("\n\n") if frame.startswith("event: done"))
                done = json.loads(frame.split("data: ", 1)[1])
                feedback = await client.post("/api/feedback", headers=headers,
                                             json={"chat_log_id": done["chat_log_id"], "category": "answer"})
                assert feedback.status_code == 201
        finally:
            server.app.dependency_overrides.clear()
            await server.engine.dispose()
        print("PASS actual registered app, real JWT authentication, owned /chat and compatible feedback")

        # Close/rebuild the saver pool: history must survive the pool lifetime.
        config = {"configurable": {"thread_id": make_thread_id(str(owner), str(sid))}}
        await pool.close()
        pool = AsyncConnectionPool(**pool_options)
        await pool.open(wait=True)
        restored = await AsyncPostgresSaver(pool).aget_tuple(config)
        assert len(restored.checkpoint["channel_values"]["messages"]) == 4
        print("PASS checkpoint read after pool rebuild")
    finally:
        await lock_pool.close()
        await pool.close()
        await engine.dispose()


def main():
    raw = os.environ.get("DATABASE_URL") or dotenv_values(ROOT / ".env").get("DATABASE_URL")
    if not raw:
        raise RuntimeError("DATABASE_URL is required for this explicit integration check")
    url = make_url(raw)
    schema = "verify_chat_storage_" + uuid.uuid4().hex[:12]
    with psycopg.connect(url.set(drivername="postgresql").render_as_string(hide_password=False), connect_timeout=8, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            asyncio.run(verify(url, schema))
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            print("CLEANUP isolated schema removed; public tables were not modified")


if __name__ == "__main__":
    main()
