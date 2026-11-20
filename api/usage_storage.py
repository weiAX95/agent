"""M3 模型调用与供应商用量回执的事务操作。"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import GatewayModelCall, GatewayPriceVersion, GatewayUsageEvent
from src.agent.usage_pricing import PriceVersion, TokenUsage, calculate_cost


def _matches_receipt(event: GatewayUsageEvent, status: str, usage: TokenUsage,
                     source: str, is_estimated: bool) -> bool:
    return (event.status == status and event.input_tokens == usage.input_tokens
            and event.output_tokens == usage.output_tokens
            and event.cached_input_tokens == usage.cached_input_tokens
            and event.source == source and event.is_estimated == is_estimated)


async def start_model_call(
    db: AsyncSession, run_id: uuid.UUID, owner_id: uuid.UUID,
    provider: str, model: str, trace_id: str | None = None,
    now: datetime | None = None,
) -> GatewayModelCall:
    """在发出物理请求前登记独立 call_id；调用方必须先提交事务。"""
    started_at = now or datetime.now(UTC)
    price = await db.scalar(
        select(GatewayPriceVersion).where(
            GatewayPriceVersion.provider == provider,
            GatewayPriceVersion.model == model,
            GatewayPriceVersion.effective_at <= started_at,
        ).order_by(GatewayPriceVersion.effective_at.desc(), GatewayPriceVersion.id.desc()).limit(1)
    )
    call = GatewayModelCall(
        id=uuid.uuid4(), run_id=run_id, owner_id=owner_id,
        provider=provider, model=model, trace_id=trace_id,
        started_at=started_at, status="started",
        # 价格只在调用开始时选一次；后来补录的历史价格不能改变本次账单。
        price_version_id=price.id if price else None,
    )
    db.add(call)
    await db.flush()
    return call


async def finish_model_call(
    db: AsyncSession, call_id: uuid.UUID, status: str,
    upstream_request_id: str | None = None,
    now: datetime | None = None,
) -> bool:
    """只允许 started 转为一次终态；迟到回执由独立事件表接收。"""
    if status not in {"succeeded", "failed", "cancelled", "timed_out", "unknown"}:
        raise ValueError("模型调用终态无效")
    result = await db.execute(
        update(GatewayModelCall).where(
            GatewayModelCall.id == call_id,
            GatewayModelCall.status == "started",
        ).values(status=status, upstream_request_id=upstream_request_id,
                 finished_at=now or datetime.now(UTC))
    )
    return result.rowcount == 1


async def record_usage_receipt(
    db: AsyncSession, call_id: uuid.UUID, event_key: str,
    status: str, usage: TokenUsage, source: str,
    is_estimated: bool = False,
) -> GatewayUsageEvent:
    """按供应商回执键幂等入账；同键不同内容必须报告冲突。"""
    if not event_key or len(event_key) > 128 or not source or len(source) > 32:
        raise ValueError("回执标识或来源无效")
    if status not in {"succeeded", "failed", "cancelled", "timed_out", "unknown"}:
        raise ValueError("回执状态无效")
    call = await db.get(GatewayModelCall, call_id)
    if call is None:
        raise ValueError("物理模型调用不存在")
    existing = await db.scalar(select(GatewayUsageEvent).where(
        GatewayUsageEvent.call_id == call_id, GatewayUsageEvent.event_key == event_key,
    ))
    if existing is not None:
        if not _matches_receipt(existing, status, usage, source, is_estimated):
            raise ValueError("同一回执键对应不同用量或费用")
        return existing
    price = await db.get(GatewayPriceVersion, call.price_version_id) if call.price_version_id else None
    snapshot = (PriceVersion(
        price.id, price.currency, price.input_per_million,
        price.output_per_million, price.cached_input_per_million,
    ) if price else None)
    cost = calculate_cost(usage, snapshot) if snapshot else None
    event = GatewayUsageEvent(
        id=uuid.uuid4(), call_id=call_id, event_key=event_key,
        status=status, input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        price_version_id=price.id if cost is not None else None,
        currency=price.currency if cost is not None else None,
        cost=cost, source=source, is_estimated=is_estimated,
    )
    # 保存点仅回滚冲突插入，保留调用方事务中的 Run 与消息修改。
    try:
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError:
        existing = await db.scalar(select(GatewayUsageEvent).where(
            GatewayUsageEvent.call_id == call_id,
            GatewayUsageEvent.event_key == event_key,
        ))
        if existing is None:
            raise
        if not _matches_receipt(existing, status, usage, source, is_estimated):
            raise ValueError("同一回执键对应不同用量或费用")
        return existing
    return event
