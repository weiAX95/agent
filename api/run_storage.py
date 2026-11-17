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
    result = await db.execute(
        update(AgentRun).where(AgentRun.id == run_id, AgentRun.status == expected)
        .values(**values)
    )
    return result.rowcount == 1
