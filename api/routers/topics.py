import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.db import get_db
from api.models import LearningPath, Topic

router = APIRouter(prefix="/api", tags=["topics"])


class CreateTopicRequest(BaseModel):
    path_id: uuid.UUID
    title: str
    order_index: int = 0


@router.get("/paths/{path_id}/topics")
async def list_topics(
    path_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 先校验父路径归属
    path = await db.get(LearningPath, path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="路径不存在")
    if path.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问此路径")

    stmt = select(Topic).where(Topic.path_id == path_id).order_by(Topic.order_index)
    result = await db.execute(stmt)
    return {"code": 0, "data": result.scalars().all(), "message": "ok"}


@router.post("/topics")
async def create_topic(
    req: CreateTopicRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 校验父路径归属
    path = await db.get(LearningPath, req.path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="路径不存在")
    if path.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权在此路径下创建主题")

    topic = Topic(**req.model_dump())
    db.add(topic)
    await db.commit()
    await db.refresh(topic)
    return {"code": 0, "data": topic, "message": "ok"}
