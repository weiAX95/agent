import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.agent.checkpoint import (
    CHECKPOINT_SETUP_LOCK_ID,
    checkpoint_dsn,
    checkpoint_lock_id,
)
from src.agent.main import build_agent, chat_stream

from api.config import settings
from api.db import Base, check_db_connection, engine, get_db
from api.models import ChatLog
from api.routers import paths, tasks, topics

logger = logging.getLogger(__name__)


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
        app.state.agent = build_agent(checkpointer)
        yield
    finally:
        if pool is not None:
            await pool.close()
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

    event_queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
    usage_callback = UsageMetadataCallbackHandler()

    async def publish_token(token: str) -> None:
        await event_queue.put(("token", token))

    print("session_id:", request.session_id, "user_id:", request.user_id)

    async def run_chat() -> tuple[str, str | None]:
        try:
            status, answer = await chat_stream(
                message=request.message,
                user_id=request.user_id,
                session_id=request.session_id,
                agent=http_request.app.state.agent,
                on_token=publish_token,
                callbacks=[usage_callback],
            )
            await event_queue.put(("usage", _usage_payload(usage_callback)))
            await event_queue.put((status, answer))
            return status, answer
        except asyncio.CancelledError:
            raise
        except Exception:
            # The response has started by this point, so report failures as SSE events.
            logger.exception(
                "Chat stream failed (user_id=%s, session_id=%s)",
                request.user_id,
                request.session_id,
            )
            await event_queue.put(("usage", _usage_payload(usage_callback)))
            await event_queue.put(("error", "生成回复失败，请稍后重试。"))
            return "error", "生成回复失败，请稍后重试。"

    log_started = False

    async def persist_chat_log(answer: str) -> None:
        nonlocal log_started
        log_started = True
        db.add(
            ChatLog(
                user_id=request.user_id,
                session_id=request.session_id,
                message=request.message,
                answer=answer,
            )
        )
        await db.commit()

    def encode_event(name: str, payload: dict) -> str:
        data = json.dumps(payload, ensure_ascii=False)
        return f"event: {name}\ndata: {data}\n\n"

    async def event_stream():
        producer = asyncio.create_task(run_chat())
        try:
            yield ": connected\n\n"
            while True:
                kind, payload = await event_queue.get()
                if kind == "token":
                    yield encode_event("token", {"content": payload})
                    continue
                if kind == "usage":
                    yield encode_event("usage", payload)
                    continue

                await producer
                if kind == "error":
                    await db.rollback()
                    yield encode_event("error", {"message": payload})
                    return

                try:
                    answer = payload
                    if kind == "stopped":
                        answer = (payload or "") + "\n\n[本轮已停止生成]"
                    await persist_chat_log(answer)
                except Exception:
                    logger.exception(
                        "Saving chat log failed (user_id=%s, session_id=%s)",
                        request.user_id,
                        request.session_id,
                    )
                    await db.rollback()
                    yield encode_event(
                        "error", {"message": "保存对话记录失败，请稍后重试。"}
                    )
                    return
                if kind == "stopped":
                    yield encode_event("stopped", {"answer": answer})
                else:
                    yield encode_event("done", {"answer": answer})
                return
        except asyncio.CancelledError:
            # Propagate disconnect cancellation into LangGraph's async model stream.
            producer.cancel()
            try:
                kind, answer = await asyncio.shield(producer)
                if kind in ("done", "stopped") and answer is not None:
                    if not log_started:
                        if kind == "stopped":
                            answer = (answer or "") + "\n\n[本轮已停止生成]"
                        await persist_chat_log(answer)
                    else:
                        await db.rollback()
                else:
                    await db.rollback()
            except Exception:
                logger.exception(
                    "Finalizing disconnected chat failed (user_id=%s, session_id=%s)",
                    request.user_id,
                    request.session_id,
                )
                await db.rollback()
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
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
