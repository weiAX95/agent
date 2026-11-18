"""待核对 Run 的只读证据快照；工具结果未知时禁止自动重放。"""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import AgentRun, AgentRunReconciliation, ChatMessage, ChatSession
from src.agent.checkpoint import checkpoint_lock_id, make_thread_id
from src.agent.run_policy import RunStatus


async def inspect_run_recovery(db: AsyncSession, checkpointer, owner_id: uuid.UUID,
                               run_id: uuid.UUID) -> dict:
    """仅返回执行元数据，不泄露 checkpoint、工具输出或原始聊天内容。"""
    run = await db.get(AgentRun, run_id)
    if run is None or run.owner_id != owner_id:
        raise HTTPException(404, "Run 不存在或不可访问")
    if not run.recovery_required:
        raise HTTPException(409, "Run 不处于待核对状态")
    session = await db.get(ChatSession, run.session_id)
    message = await db.get(ChatMessage, run.assistant_message_id)
    if session is None or message is None:
        raise HTTPException(409, "Run 关联记录不完整，需要人工核对")
    if session.owner_id != owner_id or message.session_id != session.id:
        raise HTTPException(409, "Run 归属关系不一致，需要人工核对")
    thread_id = make_thread_id(str(owner_id), str(session.id))
    if session.checkpoint_thread_id != thread_id:
        raise HTTPException(409, "checkpoint 线程映射不一致，需要人工核对")
    checkpoint = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    # 只汇总可用于对账的标识和数量；真实工具副作用仍须向外部系统查证。
    checkpoint_id = (checkpoint.checkpoint.get("id") if checkpoint else None)
    parent_id = (checkpoint.parent_config or {}).get("configurable", {}).get("checkpoint_id") if checkpoint else None
    return {
        "run_id": str(run.id), "request_id": str(run.request_id),
        "session_id": str(session.id), "assistant_message_id": str(message.id),
        "run_status": run.status, "message_status": message.status,
        "message_error_code": message.error_code,
        "steps": run.steps, "max_steps": run.max_steps,
        "checkpoint_id": checkpoint_id, "parent_checkpoint_id": parent_id,
        "pending_checkpoint_writes": len(checkpoint.pending_writes) if checkpoint else 0,
        "external_effects_verified": False,
    }


async def abandon_run_after_review(
    db: AsyncSession, checkpointer, owner_id: uuid.UUID, run_id: uuid.UUID,
    reviewer_id: uuid.UUID, expected_checkpoint_id: str | None,
    external_effect_status: str, evidence_ref: str,
) -> dict:
    """人工确认外部结果后封存 Run；旧会话仍被阻断，不自动重放或清理 checkpoint。"""
    if external_effect_status not in {"none", "confirmed"}:
        raise ValueError("外部副作用必须已核实为 none 或 confirmed")
    if not evidence_ref.strip() or len(evidence_ref) > 2048:
        raise ValueError("必须提供可追溯且不超过 2048 字符的证据引用")
    run = await db.get(AgentRun, run_id)
    if run is None or run.owner_id != owner_id:
        raise HTTPException(404, "Run 不存在或不可访问")
    key = checkpoint_lock_id(str(owner_id), str(run.session_id))
    try:
        # 与聊天执行使用同一 advisory key；有 Worker 活跃时禁止人工裁决。
        acquired = await db.scalar(
            text("SELECT pg_try_advisory_xact_lock(CAST(:key AS BIGINT))"), {"key": key},
        )
        if not acquired:
            raise HTTPException(409, "会话仍在执行，不能裁决")
        await db.refresh(run)
        snapshot = await inspect_run_recovery(db, checkpointer, owner_id, run_id)
        if snapshot["run_status"] not in {RunStatus.CREATED, RunStatus.RUNNING}:
            raise HTTPException(409, "Run 已结束，不能重复裁决")
        if (snapshot["message_status"] != "interrupted"
                or snapshot["message_error_code"] != "CHECKPOINT_RECOVERY_REQUIRED"):
            raise HTTPException(409, "消息尚未标记中断，不能裁决")
        if (snapshot["checkpoint_id"] != expected_checkpoint_id
                or snapshot["pending_checkpoint_writes"]):
            raise HTTPException(409, "checkpoint 已变化或仍有待写入任务")
        now = datetime.now(UTC)
        result = await db.execute(update(AgentRun).where(
            AgentRun.id == run_id, AgentRun.owner_id == owner_id,
            AgentRun.status.in_((RunStatus.CREATED, RunStatus.RUNNING)),
            AgentRun.recovery_required.is_(True),
        ).values(status=RunStatus.FAILED, recovery_required=False,
                 finished_at=now, updated_at=now))
        if result.rowcount != 1:
            raise HTTPException(409, "Run 状态已变化，不能裁决")
        db.add(AgentRunReconciliation(
            run_id=run_id, reviewer_id=reviewer_id,
            checkpoint_id=expected_checkpoint_id,
            external_effect_status=external_effect_status,
            evidence_ref=evidence_ref.strip(),
        ))
        await db.commit()
        return {"run_id": str(run_id), "status": RunStatus.FAILED,
                "session_reusable": False}
    except BaseException:
        await db.rollback()
        raise
