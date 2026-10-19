import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.db import get_db
from api.models import LearningPath, Task, Topic

router = APIRouter(prefix="/api", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    topic_id: uuid.UUID
    title: str
    description: str | None = None
    priority: str = "medium"


class UpdateStatusRequest(BaseModel):
    status: str  # todo | in_progress | done


async def _verify_task_ownership(
    task_id: uuid.UUID, user_id: str, db: AsyncSession
) -> Task:
    """校验任务归属，返回 Task 或抛出 HTTPException。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 沿外键链路上溯校验
    topic = await db.get(Topic, task.topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="任务所属主题不存在")
    path = await db.get(LearningPath, topic.path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="任务所属路径不存在")
    if path.owner_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问此任务")
    return task


@router.get("/topics/{topic_id}/tasks")
async def list_tasks(
    topic_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 校验 topic 的父级归属
    topic = await db.get(Topic, topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="主题不存在")
    path = await db.get(LearningPath, topic.path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="主题所属路径不存在")
    if path.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问此主题")

    stmt = select(Task).where(Task.topic_id == topic_id).order_by(Task.created_at)
    result = await db.execute(stmt)
    return {"code": 0, "data": result.scalars().all(), "message": "ok"}


@router.post("/tasks")
async def create_task(
    req: CreateTaskRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 校验 topic 归属
    topic = await db.get(Topic, req.topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="主题不存在")
    path = await db.get(LearningPath, topic.path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="主题所属路径不存在")
    if path.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权在此主题下创建任务")

    task = Task(**req.model_dump())
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return {"code": 0, "data": task, "message": "ok"}


@router.patch("/tasks/{task_id}/status")
async def update_task_status(
    task_id: uuid.UUID,
    req: UpdateStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    task = await _verify_task_ownership(task_id, current_user["user_id"], db)

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
