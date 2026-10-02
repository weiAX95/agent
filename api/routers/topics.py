import uuid

from api.db import get_db
from fastapi import APIRouter, Depends
from api.models import Topic
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api", tags=["topics"])


class CreateTopicRequest(BaseModel):
    path_id: uuid.UUID
    title: str
    order_index: int = 0


@router.get("/paths/{path_id}/topics")
async def list_topics(path_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    stmt = select(Topic).where(Topic.path_id == path_id).order_by(Topic.order_index)
    result = await db.execute(stmt)
    return {"code": 0, "data": result.scalars().all(), "message": "ok"}


@router.post("/topics")
async def create_topic(req: CreateTopicRequest, db: AsyncSession = Depends(get_db)):
    topic = Topic(**req.model_dump())
    db.add(topic)
    await db.commit()
    await db.refresh(topic)
    return {"code": 0, "data": topic, "message": "ok"}
