"""Authenticated, read-only inspection of one chat request's gateway execution."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.chat_storage import owned_session
from api.db import get_db
from api.models import AgentRun, ChatMessage, ChatRunEvent, ChatSession, GatewayModelCall, GatewayUsageEvent
from api.routers.sessions import owner_id
from api.usage_storage import summarize_run_usage

router = APIRouter(prefix="/api/sessions", tags=["gateway-observability"])
recent_router = APIRouter(prefix="/api/runs", tags=["gateway-observability"])


def _iso(value):
    return value.isoformat() if value is not None else None


@recent_router.get("/recent")
async def recent_runs(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """只列当前用户仍可访问的会话，供诊断时找到 session_id 与 request_id。"""
    owner = owner_id(current_user)
    runs = (await db.scalars(select(AgentRun).join(
        ChatSession, ChatSession.id == AgentRun.session_id,
    ).where(
        AgentRun.owner_id == owner,
        ChatSession.owner_id == owner,
        ChatSession.deleted_at.is_(None),
    ).order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit))).all()
    return {"code": 0, "data": {"items": [{
        "run_id": str(run.id), "session_id": str(run.session_id),
        "request_id": str(run.request_id), "status": run.status,
        "recovery_required": run.recovery_required,
        "created_at": _iso(run.created_at), "finished_at": _iso(run.finished_at),
    } for run in runs]}, "message": "ok"}


@router.get("/{session_id}/requests/{request_id}/diagnostics")
async def request_diagnostics(
    session_id: uuid.UUID,
    request_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """按会话归属读取 Run、步骤和物理调用；不返回提示词、回复或审计证据。"""
    owner = owner_id(current_user)
    await owned_session(db, owner, session_id)
    run = await db.scalar(select(AgentRun).where(
        AgentRun.owner_id == owner,
        AgentRun.session_id == session_id,
        AgentRun.request_id == request_id,
    ))
    if run is None:
        raise HTTPException(404, "请求不存在")

    message = await db.get(ChatMessage, run.assistant_message_id)
    steps = (await db.scalars(select(ChatRunEvent).where(
        ChatRunEvent.message_id == run.assistant_message_id,
    ).order_by(ChatRunEvent.sequence))).all()
    calls = (await db.scalars(select(GatewayModelCall).where(
        GatewayModelCall.run_id == run.id,
        GatewayModelCall.owner_id == owner,
    ).order_by(GatewayModelCall.started_at, GatewayModelCall.id))).all()
    receipts = (await db.scalars(select(GatewayUsageEvent).where(
        GatewayUsageEvent.call_id.in_([call.id for call in calls]),
    ).order_by(GatewayUsageEvent.created_at, GatewayUsageEvent.id))).all() if calls else []
    receipts_by_call = {call.id: [] for call in calls}
    for receipt in receipts:
        receipts_by_call[receipt.call_id].append(receipt)
    summary, selected_by_call = summarize_run_usage(calls, receipts)

    call_data = []
    for call in calls:
        events = receipts_by_call[call.id]
        # 汇总与明细共享被选中的确认回执；未知费用不能显示为零。
        selected = selected_by_call.get(call.id)
        duration_ms = (int((call.finished_at - call.started_at).total_seconds() * 1000)
                       if call.finished_at is not None else None)
        call_data.append({
            "call_id": str(call.id), "provider": call.provider, "model": call.model,
            "status": call.status, "trace_id": call.trace_id,
            "upstream_request_id": call.upstream_request_id,
            "started_at": _iso(call.started_at), "finished_at": _iso(call.finished_at),
            "duration_ms": duration_ms, "price_version_id": call.price_version_id,
            "selected_cost_event_id": str(selected.id) if selected else None,
            "usage_events": [{
                "event_id": str(event.id), "status": event.status, "source": event.source,
                "input_tokens": event.input_tokens, "output_tokens": event.output_tokens,
                "cached_input_tokens": event.cached_input_tokens,
                "cost": str(event.cost) if event.cost is not None else None,
                "currency": event.currency, "is_estimated": event.is_estimated,
                "created_at": _iso(event.created_at),
            } for event in events],
        })

    return {"code": 0, "data": {
        "run": {
            "run_id": str(run.id), "session_id": str(run.session_id),
            "request_id": str(run.request_id), "status": run.status,
            "recovery_required": run.recovery_required,
            "steps": run.steps, "max_steps": run.max_steps,
            "created_at": _iso(run.created_at), "finished_at": _iso(run.finished_at),
            "deadline_at": _iso(run.deadline_at),
            "message_status": message.status if message else None,
            "error_code": message.error_code if message else None,
            "model_profile": message.model_profile if message else None,
        },
        "steps": [{
            "sequence": step.sequence, "stage": step.stage, "status": step.status,
            "summary": step.summary, "created_at": _iso(step.created_at),
        } for step in steps],
        "calls": call_data,
        "summary": {
            "call_count": summary.call_count,
            "priced_call_count": summary.priced_call_count,
            "unknown_cost_call_count": summary.unknown_cost_call_count,
            "costs_by_currency": {currency: str(cost) for currency, cost
                                  in summary.costs_by_currency.items()},
        },
    }, "message": "ok"}
