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
from src.agent.main import build_agent, chat_stream, close_agent_resources
from src.agent.model_gateway import available_profiles, default_profile_name

from api.auth import get_current_user
from api.chat_storage import begin_turn, owned_session, session_execution_lock
from api.chat_stream import ChatRequest, persisted_chat_response
from api.config import settings
from api.gateway import ChatBodyLimitMiddleware
from api.base import Base
from api.db import check_db_connection, engine, get_db
from api.routers import auth, feedback, knowledge, paths, run_diagnostics, sessions, tasks, topics

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

    profiles = available_profiles()
    default_profile = default_profile_name()

    pool = None
    lock_pool = None
    try:
        async with engine.begin() as conn:
            # Coordinate first-time schema migration across API workers.
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:lock_id AS BIGINT))"),
                {"lock_id": CHECKPOINT_SETUP_LOCK_ID},
            )
            # Run 及人工裁决表必须先执行版本化 SQL 迁移；create_all 不能替代正式迁移。
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[table for table in Base.metadata.sorted_tables
                        if table.name not in {
                            "agent_runs", "agent_run_reconciliations",
                            "gateway_price_versions", "gateway_model_calls", "gateway_usage_events",
                            "gateway_call_reconciliations",
                            "gateway_budget_accounts", "gateway_budget_reservations",
                        }],
            )
            if (await conn.scalar(text("SELECT to_regclass('public.agent_runs')")) is None
                    or await conn.scalar(text("SELECT to_regclass('public.agent_run_reconciliations')")) is None):
                raise RuntimeError("请先执行 migrations/20261117_agent_runs.sql")
            # 计量启用后，缺少账本或增量迁移时拒绝启动，避免实际调用绕过持久记录。
            if not await conn.scalar(text(
                "SELECT to_regclass('public.gateway_price_versions') IS NOT NULL "
                "AND to_regclass('public.gateway_model_calls') IS NOT NULL "
                "AND to_regclass('public.gateway_usage_events') IS NOT NULL "
                "AND to_regclass('public.gateway_call_reconciliations') IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM information_schema.columns WHERE "
                "table_schema = 'public' AND table_name = 'gateway_model_calls' "
                "AND column_name = 'price_version_id') "
                "AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'gateway_price_append_only') "
                "AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'gateway_usage_append_only')"
                "AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'gateway_call_reconciliation_append_only')"
            )):
                raise RuntimeError("请先执行全部 M3 gateway_usage、call_price_snapshot、ledger_append_only 和 call_reconciliations 迁移")
            await conn.execute(text(
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS "
                "model_profile VARCHAR(32) NOT NULL DEFAULT 'default'"
            ))
            if "default" not in {item["id"] for item in profiles}:
                await conn.execute(
                    text("UPDATE chat_messages SET model_profile = :profile "
                         "WHERE model_profile = 'default'"),
                    {"profile": default_profile},
                )
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
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "prepare_threshold": 0,
            },
            open=False,
        )
        await lock_pool.open(wait=True)
        app.state.chat_lock_pool = lock_pool
        app.state.model_profiles = profiles
        app.state.default_model_profile = default_profile
        app.state.agent = build_agent(checkpointer)
        app.state.agents = {default_profile: app.state.agent}
        for profile in profiles:
            name = profile["id"]
            if name != default_profile:
                app.state.agents[name] = build_agent(checkpointer, name, initialize=False)
        yield
    finally:
        if lock_pool is not None:
            await lock_pool.close()
        if pool is not None:
            await pool.close()
        try:
            close_agent_resources()
        finally:
            await engine.dispose()


app = FastAPI(lifespan=lifespan)  # 关键：lifespan必须在这里传进去，否则永远不会执行
app.add_middleware(ChatBodyLimitMiddleware)

app.include_router(paths.router)
app.include_router(topics.router)
app.include_router(tasks.router)
app.include_router(auth.router)
app.include_router(feedback.router)
app.include_router(knowledge.router)
app.include_router(sessions.router)
app.include_router(run_diagnostics.router)
app.include_router(run_diagnostics.recent_router)


@app.get("/api/models")
async def list_model_profiles(
    request: Request, current_user: dict = Depends(get_current_user),
):
    return {"code": 0, "data": {
        "items": request.app.state.model_profiles,
        "default": request.app.state.default_model_profile,
    }, "message": "ok"}


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
    profile = request.model_profile or getattr(http_request.app.state, "default_model_profile", "default")
    agents = getattr(http_request.app.state, "agents", None)
    if agents is not None and profile not in agents:
        raise HTTPException(422, "模型档位不存在")
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
        turn = await begin_turn(
            db, owner, request.session_id, request.request_id, request.message, profile
        )
        turn.model_profile = profile
        try:
            yield turn
        finally:
            import anyio

            with anyio.CancelScope(shield=True):
                try:
                    await turn.interrupt_if_streaming()
                except Exception:
                    logger.exception(
                        "Could not finalize interrupted request_id=%s",
                        request.request_id,
                    )


@app.post("/api/chat")
async def chat_api(
    request: ChatRequest,
    http_request: Request,
    turn=Depends(get_chat_turn, scope="request"),
):
    # Request-scoped dependency retains the execution lock until SSE closes.
    return persisted_chat_response(
        turn, getattr(http_request.app.state, "agents", {}).get(
            turn.model_profile, http_request.app.state.agent
        ), chat_stream, _usage_payload, turn.model_profile,
        getattr(http_request, "scope", {}).get("gateway_trace_id"),
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
