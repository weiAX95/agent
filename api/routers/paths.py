import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.db import get_db
from api.models import LearningPath

router = APIRouter(prefix="/api/paths", tags=["paths"])


class CreatePathRequest(BaseModel):
    title: str
    description: str | None = None
    expected_weeks: int | None = None


class UpdatePathRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None


@router.get("")
async def list_paths(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    stmt = select(LearningPath).where(
        LearningPath.deleted_at.is_(None),
        LearningPath.owner_id == current_user["user_id"],  # ← 按用户过滤
    )
    if status:
        stmt = stmt.where(LearningPath.status == status)
    result = await db.execute(stmt)
    return {"code": 0, "data": result.scalars().all(), "message": "ok"}


@router.post("")
async def create_path(
    req: CreatePathRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    path = LearningPath(**req.model_dump(), owner_id=current_user["user_id"])
    db.add(path)
    await db.commit()
    await db.refresh(path)
    return {"code": 0, "data": path, "message": "ok"}


@router.patch("/{path_id}")
async def update_path(
    path_id: uuid.UUID,
    req: UpdatePathRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),  # ← 新增
):
    path = await db.get(LearningPath, path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="路径不存在")
    if path.owner_id != current_user["user_id"]:  # ← 所有权校验
        raise HTTPException(status_code=403, detail="无权访问此路径")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(path, field, value)
    await db.commit()
    await db.refresh(path)
    return {"code": 0, "data": path, "message": "ok"}


@router.delete("/{path_id}")
async def delete_path(
    path_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    path = await db.get(LearningPath, path_id)
    if not path or path.deleted_at is not None:
        raise HTTPException(status_code=404, detail="路径不存在")
    if path.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权删除此路径")
    path.deleted_at = datetime.now(UTC)
    await db.commit()
    return {"code": 0, "data": None, "message": "ok"}
