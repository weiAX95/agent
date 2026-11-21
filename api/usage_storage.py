"""M3 模型调用与供应商用量回执的事务操作。"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import AgentRun, GatewayModelCall, GatewayPriceVersion, GatewayUsageEvent
from api.run_storage import mark_run_recovery_required
from src.agent.usage_pricing import PriceVersion, TokenUsage, calculate_cost


@dataclass(frozen=True)
class RunUsageSummary:
    call_count: int
    priced_call_count: int
    unknown_cost_call_count: int
    costs_by_currency: dict[str, Decimal]


async def reconcile_stale_model_calls(db: AsyncSession, now: datetime | None = None,
                                      grace_seconds: int = 300,
                                      limit: int = 100) -> list[uuid.UUID]:
    """只标记超过 Run 截止时间的失联请求；保留迟到供应商回执的入账空间。"""
    if type(grace_seconds) is not int or grace_seconds < 0 or not 1 <= limit <= 1000:
        raise ValueError("失联核对参数无效")
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=grace_seconds)
    calls = (await db.scalars(
        select(GatewayModelCall).join(AgentRun, AgentRun.id == GatewayModelCall.run_id)
        .where(GatewayModelCall.status == "started",
               GatewayModelCall.started_at <= cutoff,
               AgentRun.deadline_at <= cutoff)
        .order_by(GatewayModelCall.started_at, GatewayModelCall.id)
        .limit(limit)
        .with_for_update(of=GatewayModelCall, skip_locked=True)
    )).all()
    reconciled = []
    for call in calls:
        if not await finish_model_call(db, call.id, "unknown", now=now):
            continue
        await mark_run_recovery_required(db, call.run_id)
        # 这个事件仅证明执行结果待核对，不推算 token 或费用，也不重发请求。
        db.add(GatewayUsageEvent(
            id=uuid.uuid4(), call_id=call.id, event_key="recovery",
            status="unknown", source="recovery", created_at=now,
        ))
        reconciled.append(call.id)
    await db.flush()
    return reconciled


async def get_run_usage_summary(db: AsyncSession, run_id: uuid.UUID,
                                owner_id: uuid.UUID) -> RunUsageSummary:
    """按物理调用选最新已确认回执，避免重复回执或修正记录造成重复计费。"""
    calls = (await db.scalars(select(GatewayModelCall).where(
        GatewayModelCall.run_id == run_id,
        GatewayModelCall.owner_id == owner_id,
    ))).all()
    if not calls:
        return RunUsageSummary(0, 0, 0, {})
    events = (await db.scalars(select(GatewayUsageEvent).where(
        GatewayUsageEvent.call_id.in_([call.id for call in calls]),
        GatewayUsageEvent.is_estimated.is_(False),
        GatewayUsageEvent.cost.is_not(None),
    ).order_by(GatewayUsageEvent.created_at.desc(), GatewayUsageEvent.id.desc()))).all()
    # 一次物理请求只选一张已确认账单；失败或取消调用若收到真实费用也计入。
    chosen = {}
    for event in events:
        chosen.setdefault(event.call_id, event)
    totals: dict[str, Decimal] = {}
    for event in chosen.values():
        if event.currency is None:
            raise ValueError("已计费回执缺少币种")
        totals[event.currency] = totals.get(event.currency, Decimal(0)) + event.cost
    return RunUsageSummary(len(calls), len(chosen), len(calls) - len(chosen), totals)


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
    now: datetime | None = None,
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
        created_at=now or datetime.now(UTC),
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
