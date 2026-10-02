import uuid

from api.db import get_db
from fastapi import APIRouter, Depends, HTTPException
from api.models import LearningPath
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
async def list_paths(status: str | None = None, db: AsyncSession = Depends(get_db)):
    stmt = select(LearningPath).where(LearningPath.deleted_at.is_(None))
    if status:
        stmt = stmt.where(LearningPath.status == status)
    result = await db.execute(stmt)
    return {"code": 0, "data": result.scalars().all(), "message": "ok"}


@router.post("")
async def create_path(req: CreatePathRequest, db: AsyncSession = Depends(get_db)):
    path = LearningPath(**req.model_dump())
    db.add(path)
    await db.commit()
    await db.refresh(path)
    return {"code": 0, "data": path, "message": "ok"}


@router.patch("/{path_id}")
async def update_path(
    path_id: uuid.UUID, req: UpdatePathRequest, db: AsyncSession = Depends(get_db)
):
    path = await db.get(LearningPath, path_id)
    if not path:
        raise HTTPException(status_code=404, detail="路径不存在")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(path, field, value)
    await db.commit()
    await db.refresh(path)
    return {"code": 0, "data": path, "message": "ok"}
