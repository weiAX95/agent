import uuid
from datetime import UTC, datetime

from api.db import get_db
from fastapi import APIRouter, Depends, HTTPException
from api.models import Task
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    topic_id: uuid.UUID
    title: str
    description: str | None = None
    priority: str = "medium"


class UpdateStatusRequest(BaseModel):
    status: str  # todo | in_progress | done


@router.get("/topics/{topic_id}/tasks")
async def list_tasks(topic_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    stmt = select(Task).where(Task.topic_id == topic_id).order_by(Task.created_at)
    result = await db.execute(stmt)
    return {"code": 0, "data": result.scalars().all(), "message": "ok"}


@router.post("/tasks")
async def create_task(req: CreateTaskRequest, db: AsyncSession = Depends(get_db)):
    task = Task(**req.model_dump())
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return {"code": 0, "data": task, "message": "ok"}


@router.patch("/tasks/{task_id}/status")
async def update_task_status(
    task_id: uuid.UUID, req: UpdateStatusRequest, db: AsyncSession = Depends(get_db)
):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    allowed = Task.VALID_TRANSITIONS.get(task.status, set())
    if req.status not in allowed:
        raise HTTPException(
            status_code=400, detail=f"不能从 {task.status} 跳转到 {req.status}"
        )

    task.status = req.status
    task.completed_at = datetime.now(UTC) if req.status == "done" else None
    await db.commit()
    await db.refresh(task)
    return {"code": 0, "data": task, "message": "ok"}
