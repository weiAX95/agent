import logging
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPBearer
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.agent.checkpoint import (
    CHECKPOINT_SETUP_LOCK_ID,
    checkpoint_dsn,
)
from src.agent.main import build_agent, chat_stream

from api.auth import get_current_user
from api.config import settings
from api.db import Base, check_db_connection, engine, get_db
from api.chat_storage import begin_turn, owned_session, session_execution_lock
from api.chat_stream import ChatRequest, persisted_chat_response
from api.routers import auth, feedback, paths, sessions, tasks, topics

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):

    # 启动时强制校验 JWT 配置，拒绝空值启动
    if not settings.jwt_verifying_key:
        raise RuntimeError("JWT_VERIFYING_KEY 未配置。请在 .env 中设置非空密钥。")
    if not settings.jwt_issuer:
        raise RuntimeError("JWT_ISSUER 未配置。")
    if not settings.jwt_audience:
        raise RuntimeError("JWT_AUDIENCE 未配置。")

    pool = None
    lock_pool = None
    try:
        async with engine.begin() as conn:
            # Coordinate first-time schema migration across API workers.
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:lock_id AS BIGINT))"),
                {"lock_id": CHECKPOINT_SETUP_LOCK_ID},
            )
            await conn.run_sync(Base.metadata.create_all)
            pool = AsyncConnectionPool(
                conninfo=checkpoint_dsn(settings.database_url),
                min_size=1,
                max_size=10,
                kwargs={
                    "autocommit": True,
                    "row_factory": dict_row,
                    "prepare_threshold": 0,
                },
                open=False,
            )
            await pool.open(wait=True)
            checkpointer = AsyncPostgresSaver(pool)
            await checkpointer.setup()

        # Lock connections stay borrowed for the response lifetime. Keep them
        # separate so they cannot starve checkpoint operations of connections.
        lock_pool = AsyncConnectionPool(
            conninfo=checkpoint_dsn(settings.database_url),
            min_size=0,
            max_size=10,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
            open=False,
        )
        await lock_pool.open(wait=True)
        app.state.chat_lock_pool = lock_pool
        app.state.agent = build_agent(checkpointer)
        yield
    finally:
        if lock_pool is not None:
            await lock_pool.close()
        if pool is not None:
            await pool.close()
        await engine.dispose()


app = FastAPI(lifespan=lifespan)  # 关键：lifespan必须在这里传进去，否则永远不会执行

app.include_router(paths.router)
app.include_router(topics.router)
app.include_router(tasks.router)
app.include_router(auth.router)
app.include_router(feedback.router)
app.include_router(sessions.router)


bearer = HTTPBearer(auto_error=False)


@app.get("/health")
async def health():
    db_ok = await check_db_connection()
    return {"status": "ok", "database": "connected" if db_ok else "disconnected"}


async def get_chat_turn(
    request: ChatRequest,
    http_request: Request,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        owner = uuid.UUID(current_user["user_id"])
    except ValueError as exc:
        raise HTTPException(401, "用户标识无效") from exc
    # Reject unauthorized requests before consuming a lock-pool connection.
    await owned_session(db, owner, request.session_id)
    await db.rollback()
    async with session_execution_lock(
        http_request.app.state.chat_lock_pool, owner, request.session_id
    ):
        turn = await begin_turn(db, owner, request.session_id, request.request_id, request.message)
        try:
            yield turn
        finally:
            import anyio
            with anyio.CancelScope(shield=True):
                try:
                    await turn.interrupt_if_streaming()
                except Exception:
                    logger.exception("Could not finalize interrupted request_id=%s", request.request_id)


@app.post("/chat")
async def chat_api(
    request: ChatRequest,
    http_request: Request,
    turn=Depends(get_chat_turn, scope="request"),
):
    # Request-scoped dependency retains the execution lock until SSE closes.
    return persisted_chat_response(
        turn, http_request.app.state.agent, chat_stream, _usage_payload
    )


def _usage_payload(callback: UsageMetadataCallbackHandler) -> dict:
    """Expose provider-reported token counts without estimating missing usage."""
    models = {}
    for model_name, usage in callback.usage_metadata.items():
        models[model_name] = {
            key: int(usage.get(key, 0))
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }
    return {
        "available": bool(models),
        "models": models,
        "input_tokens": sum(item["input_tokens"] for item in models.values()),
        "output_tokens": sum(item["output_tokens"] for item in models.values()),
        "total_tokens": sum(item["total_tokens"] for item in models.values()),
    }
