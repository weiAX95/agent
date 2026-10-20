"""User-reported answer issues, scoped to an owned chat log."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.db import get_db
from api.models import ChatFeedback, ChatLog

router = APIRouter(prefix="/api", tags=["feedback"])


class FeedbackCreate(BaseModel):
    chat_log_id: uuid.UUID
    category: Literal["answer", "memory", "other"]


@router.post("/feedback", status_code=201)
async def submit_feedback(
    request: FeedbackCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    owned_log = await db.execute(
        select(ChatLog).where(
            ChatLog.id == request.chat_log_id,
            ChatLog.user_id == current_user["user_id"],
        )
    )
    if owned_log.scalar_one_or_none() is None:
        raise HTTPException(404, "对话记录不存在")

    feedback = ChatFeedback(
        id=uuid.uuid4(),
        chat_log_id=request.chat_log_id,
        user_id=current_user["user_id"],
        category=request.category,
    )
    db.add(feedback)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "这条回答已提交反馈") from exc
    return {"feedback_id": str(feedback.id), "status": "recorded"}
