"""物理 HTTP 请求的持久账本适配器；发送前提交调用，结束后提交未知用量回执。"""

import uuid
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from api.models import GatewayModelCall, GatewayPriceVersion, GatewayUsageEvent
from api.usage_storage import finish_model_call, record_usage_receipt, start_model_call
from src.agent.physical_transport import MeteredRun
from src.agent.usage_pricing import TokenUsage


@lru_cache(maxsize=1)
def _sync_engine():
    from api.config import settings

    url = make_url(settings.database_url)
    if url.drivername != "postgresql+asyncpg":
        raise RuntimeError("同步模型计量仅支持 PostgreSQL 账本")
    return create_engine(url.set(drivername="postgresql+psycopg"), pool_pre_ping=True)


def close_usage_sync_engine() -> None:
    if _sync_engine.cache_info().currsize:
        _sync_engine().dispose()
        _sync_engine.cache_clear()


class DatabaseAttemptSink:
    """同步 Memory 调用和异步 Agent 调用分别使用独立数据库会话。"""

    def __init__(self, sync_engine=None, async_sessions=None):
        self._provided_sync_engine = sync_engine
        self._provided_async_sessions = async_sessions

    def _engine(self):
        return self._provided_sync_engine or _sync_engine()

    def _sessions(self):
        if self._provided_async_sessions is not None:
            return self._provided_async_sessions
        from api.db import AsyncSessionLocal
        return AsyncSessionLocal

    def start(self, run: MeteredRun, provider: str, model: str) -> uuid.UUID:
        now = datetime.now(UTC)
        with Session(self._engine(), expire_on_commit=False) as db:
            price = db.scalar(select(GatewayPriceVersion).where(
                GatewayPriceVersion.provider == provider,
                GatewayPriceVersion.model == model,
                GatewayPriceVersion.effective_at <= now,
            ).order_by(GatewayPriceVersion.effective_at.desc(), GatewayPriceVersion.id.desc()).limit(1))
            call = GatewayModelCall(
                id=uuid.uuid4(), run_id=run.run_id, owner_id=run.owner_id,
                provider=provider, model=model, status="started",
                trace_id=run.trace_id, started_at=now,
                price_version_id=price.id if price else None,
            )
            db.add(call)
            db.commit()
            return call.id

    def finish(self, call_id: uuid.UUID, status: str) -> None:
        with Session(self._engine()) as db:
            updated = db.execute(update(GatewayModelCall).where(
                GatewayModelCall.id == call_id,
                GatewayModelCall.status == "started",
            ).values(status=status, finished_at=datetime.now(UTC)))
            if updated.rowcount != 1:
                raise RuntimeError("模型调用终态写入冲突")
            # 传输层没有供应商 token；明确保存未知，后续确认回执用新事件键追加。
            db.add(GatewayUsageEvent(
                id=uuid.uuid4(), call_id=call_id, event_key="transport",
                status=status, source="transport",
            ))
            db.commit()

    async def astart(self, run: MeteredRun, provider: str, model: str) -> uuid.UUID:
        async with self._sessions()() as db:
            call = await start_model_call(
                db, run.run_id, run.owner_id, provider, model, run.trace_id,
            )
            await db.commit()
            return call.id

    async def afinish(self, call_id: uuid.UUID, status: str) -> None:
        async with self._sessions()() as db:
            if not await finish_model_call(db, call_id, status):
                raise RuntimeError("模型调用终态写入冲突")
            await record_usage_receipt(
                db, call_id, "transport", status,
                TokenUsage(None, None, None), "transport",
            )
            await db.commit()
