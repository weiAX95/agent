"""Create an authenticated user's chat session; history UI is a later slice."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.chat_storage import create_session
from api.db import get_db

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", max_length=200)


@router.post("", status_code=201)
async def create_chat_session(
    request: SessionCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        owner = uuid.UUID(current_user["user_id"])
    except ValueError as exc:
        raise HTTPException(401, "用户标识无效") from exc
    session = await create_session(db, owner, request.title)
    return {"code": 0, "data": {
        "id": str(session.id), "title": session.title,
        "created_at": session.created_at.isoformat(),
    }, "message": "ok"}
