"""Run 状态持久化：所有状态转换均由数据库条件更新裁决。"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import AgentRun
from src.agent.run_policy import RunLimits, RunStatus


def new_run(owner_id: uuid.UUID, session_id: uuid.UUID, request_id: uuid.UUID,
            assistant_message_id: uuid.UUID, limits: RunLimits,
            now: datetime | None = None) -> AgentRun:
    """在消息预留事务中创建 Run；持久 Deadline 用于重启后的剩余时间计算。"""
    now = now or datetime.now(UTC)
    return AgentRun(
        id=uuid.uuid4(), owner_id=owner_id, session_id=session_id,
        request_id=request_id, assistant_message_id=assistant_message_id,
        status=RunStatus.CREATED, max_steps=limits.max_steps,
        deadline_at=now + timedelta(seconds=limits.timeout_seconds),
    )


async def transition_run(db: AsyncSession, run_id: uuid.UUID,
                         expected: RunStatus, target: RunStatus,
                         now: datetime | None = None) -> bool:
    """CAS 只允许 created→running 或 running→终态；竞争失败不覆盖已提交结果。"""
    terminals = {RunStatus.SUCCEEDED, RunStatus.FAILED,
                 RunStatus.CANCELLED, RunStatus.TIMED_OUT}
    if not ((expected == RunStatus.CREATED and target == RunStatus.RUNNING)
            or (expected == RunStatus.RUNNING and target in terminals)):
        raise ValueError("非法 Run 状态转换")
    now = now or datetime.now(UTC)
    values = {"status": target, "updated_at": now}
    if target in terminals:
        values["finished_at"] = now
    # 待核对的未知执行结果不能被迟到的 Worker 再启动或自动覆盖为终态。
    conditions = [AgentRun.id == run_id, AgentRun.status == expected,
                  AgentRun.recovery_required.is_(False)]
    result = await db.execute(update(AgentRun).where(*conditions).values(**values))
    return result.rowcount == 1


async def record_run_step(db: AsyncSession, run_id: uuid.UUID, expected_steps: int) -> bool:
    """只提交当前 Run 的下一步；状态竞争或重复回调不能多记一次。"""
    result = await db.execute(
        update(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.status == RunStatus.RUNNING,
            AgentRun.recovery_required.is_(False),
            AgentRun.steps == expected_steps - 1,
            AgentRun.max_steps >= expected_steps,
        ).values(steps=expected_steps, updated_at=datetime.now(UTC))
    )
    return result.rowcount == 1


async def mark_run_recovery_required(db: AsyncSession, run_id: uuid.UUID) -> bool:
    """保留未确认的执行事实；恢复人员核对 checkpoint 与工具结果前不裁决终态。"""
    result = await db.execute(
        update(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.status.in_((RunStatus.CREATED, RunStatus.RUNNING)),
        ).values(recovery_required=True, updated_at=datetime.now(UTC))
    )
    return result.rowcount == 1
