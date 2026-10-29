"""Create and read an authenticated user's chat sessions."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.chat_storage import create_session, owned_session
from api.db import get_db
from api.models import ChatMessage, ChatRunEvent, ChatSession

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def owner_id(current_user: dict) -> uuid.UUID:
    try:
        return uuid.UUID(current_user["user_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(401, "用户标识无效") from exc


def session_data(session: ChatSession) -> dict:
    return {
        "id": str(session.id),
        "title": session.title,
        "created_at": session.created_at.isoformat(),
        "last_message_at": session.last_message_at.isoformat() if session.last_message_at else None,
    }


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", max_length=200)


@router.post("", status_code=201)
async def create_chat_session(
    request: SessionCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await create_session(db, owner_id(current_user), request.title)
    return {"code": 0, "data": session_data(session), "message": "ok"}


@router.get("")
async def list_chat_sessions(
    limit: int = Query(default=30, ge=1, le=100),
    before_activity: datetime | None = None,
    before_id: uuid.UUID | None = None,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if (before_activity is None) != (before_id is None):
        raise HTTPException(422, "分页游标不完整")
    if before_activity is not None and before_activity.tzinfo is None:
        raise HTTPException(422, "分页时间必须包含时区")
    query = select(ChatSession).where(
        ChatSession.owner_id == owner_id(current_user),
        ChatSession.deleted_at.is_(None),
        ChatSession.last_message_at.is_not(None),
    )
    if before_activity is not None:
        query = query.where(or_(
            ChatSession.last_message_at < before_activity,
            and_(ChatSession.last_message_at == before_activity, ChatSession.id < before_id),
        ))
    rows = (await db.scalars(query.order_by(
        ChatSession.last_message_at.desc(), ChatSession.id.desc(),
    ).limit(limit + 1))).all()
    page = rows[:limit]
    last = page[-1] if len(rows) > limit else None
    return {"code": 0, "data": {
        "items": [session_data(row) for row in page],
        "next_before": {
            "activity": last.last_message_at.isoformat(), "id": str(last.id),
        } if last else None,
    }, "message": "ok"}


@router.get("/recent")
async def recent_chat_session(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the latest nonempty session. Empty abandoned sessions are skipped."""
    session = await db.scalar(
        select(ChatSession)
        .where(
            ChatSession.owner_id == owner_id(current_user),
            ChatSession.deleted_at.is_(None),
            ChatSession.last_message_at.is_not(None),
        )
        .order_by(ChatSession.last_message_at.desc(), ChatSession.id.desc())
        .limit(1)
    )
    return {"code": 0, "data": session_data(session) if session else None, "message": "ok"}


@router.get("/{session_id}/messages")
async def chat_session_messages(
    session_id: uuid.UUID,
    before_seq: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await owned_session(db, owner_id(current_user), session_id)
    query = select(ChatMessage).where(ChatMessage.session_id == session_id)
    if before_seq is not None:
        query = query.where(ChatMessage.seq < before_seq)
    rows = (await db.scalars(query.order_by(ChatMessage.seq.desc()).limit(limit + 1))).all()
    has_more = len(rows) > limit
    page = list(reversed(rows[:limit]))
    event_map = {row.id: [] for row in page if row.role == "assistant"}
    if event_map:
        events = (await db.scalars(
            select(ChatRunEvent).where(ChatRunEvent.message_id.in_(event_map))
            .order_by(ChatRunEvent.message_id, ChatRunEvent.sequence)
        )).all()
        for event in events:
            event_map[event.message_id].append({
                "id": str(event.id), "message_id": str(event.message_id),
                "sequence": event.sequence, "stage": event.stage,
                "status": event.status, "summary": event.summary,
                "sources": event.sources, "created_at": event.created_at.isoformat(),
            })
    return {
        "code": 0,
        "data": {
            "items": [
                {
                    "id": str(row.id),
                    "request_id": str(row.request_id),
                    "seq": row.seq,
                    "role": row.role,
                    "content": row.content,
                    "status": row.status,
                    "error_code": row.error_code,
                    "memory_status": row.memory_status or "unknown",
                    "events": event_map.get(row.id, []),
                }
                for row in page
            ],
            "next_before_seq": page[0].seq if has_more else None,
        },
        "message": "ok",
    }
