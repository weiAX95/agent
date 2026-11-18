"""待核对 Run 的只读证据快照；工具结果未知时禁止自动重放。"""

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import AgentRun, ChatMessage, ChatSession
from src.agent.checkpoint import make_thread_id


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
        "steps": run.steps, "max_steps": run.max_steps,
        "checkpoint_id": checkpoint_id, "parent_checkpoint_id": parent_id,
        "pending_checkpoint_writes": len(checkpoint.pending_writes) if checkpoint else 0,
        "external_effects_verified": False,
    }
