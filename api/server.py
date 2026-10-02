import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from langgraph.checkpoint.postgres import PostgresSaver
from pydantic import BaseModel
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.agent.checkpoint import (
    CHECKPOINT_SETUP_LOCK_ID,
    checkpoint_dsn,
    checkpoint_lock_id,
)
from src.agent.main import build_agent, chat

from api.config import settings
from api.db import Base, check_db_connection, engine, get_db
from api.models import ChatLog
from api.routers import paths, tasks, topics


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = None
    try:
        async with engine.begin() as conn:
            # Coordinate first-time schema migration across API workers.
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:lock_id AS BIGINT))"),
                {"lock_id": CHECKPOINT_SETUP_LOCK_ID},
            )
            await conn.run_sync(Base.metadata.create_all)
            pool = ConnectionPool(
                conninfo=checkpoint_dsn(settings.database_url),
                min_size=1,
                max_size=10,
                kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
                open=False,
            )
            await run_in_threadpool(pool.open, wait=True)
            checkpointer = PostgresSaver(pool)
            await run_in_threadpool(checkpointer.setup)
        app.state.agent = build_agent(checkpointer)
        yield
    finally:
        if pool is not None:
            await run_in_threadpool(pool.close)
        await engine.dispose()


app = FastAPI(lifespan=lifespan)  # 关键：lifespan必须在这里传进去，否则永远不会执行

app.include_router(paths.router)
app.include_router(topics.router)
app.include_router(tasks.router)


class ChatRequest(BaseModel):
    message: str
    session_id: str
    user_id: str


@app.get("/health")
async def health():
    db_ok = await check_db_connection()
    return {"status": "ok", "database": "connected" if db_ok else "disconnected"}


@app.post("/chat")
async def chat_api(
    request: ChatRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
):
    # Hold a transaction-scoped lock through the checkpoint write and ChatLog commit.
    # PostgreSQL coordinates requests to the same thread across API workers.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(CAST(:lock_id AS BIGINT))"),
        {"lock_id": checkpoint_lock_id(request.user_id, request.session_id)},
    )
    answer = await run_in_threadpool(
        chat,
        message=request.message,
        user_id=request.user_id,
        session_id=request.session_id,
        agent=http_request.app.state.agent,
    )
    print(
        f"Request: {request.message}, Session ID: {request.session_id}, Answer: {answer}"
    )

    log = ChatLog(
        user_id=request.user_id,
        session_id=request.session_id,
        message=request.message,
        answer=answer,
    )
    db.add(log)
    await db.commit()

    return {"answer": answer}
