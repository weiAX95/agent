"""Authenticated management of a knowledge base shared by all signed-in users."""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from api.auth import get_current_user
from api.db import get_db
from api.models import KnowledgeDocument
from src.agent.knowledge_ingest import (
    MAX_UPLOAD_BYTES, DocumentValidationError, PreparedDocument, prepare_document,
)
from src.agent.knowledge_store import delete_document_chunks, index_document

router = APIRouter(prefix="/api/knowledge/documents", tags=["knowledge"])
logger = logging.getLogger(__name__)


def _actor(current_user: dict) -> uuid.UUID:
    try:
        return uuid.UUID(current_user["user_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(401, "用户标识无效") from exc


def _data(document: KnowledgeDocument) -> dict:
    return {
        "id": str(document.id),
        "filename": document.filename,
        "content_type": document.content_type,
        "size_bytes": document.size_bytes,
        "chunk_count": document.chunk_count,
        "uploaded_by": str(document.uploaded_by),
        "created_at": document.created_at.isoformat(),
        "updated_at": document.updated_at.isoformat(),
    }


async def _prepare_upload(file: UploadFile) -> PreparedDocument:
    try:
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
    finally:
        await file.close()
    try:
        return await run_in_threadpool(prepare_document, file.filename, raw)
    except DocumentValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _index_or_fail(
    document_id: uuid.UUID, revision: uuid.UUID, prepared: PreparedDocument,
) -> None:
    try:
        await run_in_threadpool(
            index_document, document_id, revision, prepared.filename, prepared.chunks,
        )
    except Exception as exc:
        logger.exception("Could not index knowledge document=%s", document_id)
        try:
            await run_in_threadpool(delete_document_chunks, document_id, revision)
        except Exception:
            logger.exception("Could not clean partial knowledge revision=%s", revision)
        raise HTTPException(503, "知识库索引暂不可用，请稍后重试") from exc


@router.get("")
async def list_documents(
    limit: int = Query(default=50, ge=1, le=100),
    before_created_at: datetime | None = None,
    before_id: uuid.UUID | None = None,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _actor(current_user)
    if (before_created_at is None) != (before_id is None):
        raise HTTPException(422, "分页游标不完整")
    if before_created_at is not None and before_created_at.tzinfo is None:
        raise HTTPException(422, "分页时间必须包含时区")
    query = select(KnowledgeDocument).where(KnowledgeDocument.deleted_at.is_(None))
    if before_created_at is not None:
        query = query.where(or_(
            KnowledgeDocument.created_at < before_created_at,
            and_(
                KnowledgeDocument.created_at == before_created_at,
                KnowledgeDocument.id < before_id,
            ),
        ))
    rows = (await db.scalars(query.order_by(
        KnowledgeDocument.created_at.desc(), KnowledgeDocument.id.desc(),
    ).limit(limit + 1))).all()
    page = rows[:limit]
    last = page[-1] if len(rows) > limit else None
    return {"code": 0, "data": {
        "items": [_data(row) for row in page],
        "next_before": {
            "created_at": last.created_at.isoformat(), "id": str(last.id),
        } if last else None,
    }, "message": "ok"}


@router.post("", status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    actor = _actor(current_user)
    prepared = await _prepare_upload(file)
    document_id, revision = uuid.uuid4(), uuid.uuid4()
    await _index_or_fail(document_id, revision, prepared)
    document = KnowledgeDocument(
        id=document_id, revision=revision, filename=prepared.filename,
        content_type=prepared.content_type, size_bytes=prepared.size_bytes,
        sha256=prepared.sha256, chunk_count=len(prepared.chunks), uploaded_by=actor,
    )
    db.add(document)
    try:
        await db.commit()
        await db.refresh(document)
    except Exception:
        # The DB result can be unknown after a connection failure. Keep orphan
        # vectors; search verifies an active DB revision before exposing them.
        await db.rollback()
        logger.exception("Could not save knowledge document metadata=%s", document_id)
        raise HTTPException(503, "文档状态待核对，请先刷新知识库列表")
    return {"code": 0, "data": _data(document), "message": "ok"}


@router.put("/{document_id}")
async def replace_document(
    document_id: uuid.UUID,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    actor = _actor(current_user)
    existing = await db.get(KnowledgeDocument, document_id)
    if existing is None or existing.deleted_at is not None:
        raise HTTPException(404, "文档不存在")
    old_revision = existing.revision
    await db.rollback()  # Do not hold a DB transaction during parsing and embedding.
    prepared = await _prepare_upload(file)
    revision = uuid.uuid4()
    await _index_or_fail(document_id, revision, prepared)

    document = await db.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.id == document_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if document is None or document.deleted_at is not None or document.revision != old_revision:
        await db.rollback()
        try:
            await run_in_threadpool(delete_document_chunks, document_id, revision)
        except Exception:
            logger.exception("Could not clean conflicting knowledge revision=%s", revision)
        raise HTTPException(409, "文档已被其他操作修改，请刷新后重试")

    document.revision = revision
    document.filename = prepared.filename
    document.content_type = prepared.content_type
    document.size_bytes = prepared.size_bytes
    document.sha256 = prepared.sha256
    document.chunk_count = len(prepared.chunks)
    document.uploaded_by = actor
    document.updated_at = datetime.now(UTC)
    try:
        await db.commit()
        await db.refresh(document)
    except Exception:
        await db.rollback()
        logger.exception("Could not switch knowledge document revision=%s", document_id)
        raise HTTPException(503, "文档状态待核对，请先刷新知识库列表")

    try:
        await run_in_threadpool(delete_document_chunks, document_id, old_revision)
    except Exception:
        # Old vectors are not exposed because retrieval checks the DB revision.
        logger.exception("Could not purge inactive knowledge revision=%s", old_revision)
    return {"code": 0, "data": _data(document), "message": "ok"}


@router.delete("/{document_id}")
async def delete_document(
    document_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _actor(current_user)
    document = await db.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.id == document_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if document is None or document.deleted_at is not None:
        raise HTTPException(404, "文档不存在")
    document.deleted_at = datetime.now(UTC)
    document.updated_at = document.deleted_at
    await db.commit()
    try:
        await run_in_threadpool(delete_document_chunks, document_id)
    except Exception:
        # The document is immediately hidden by the committed soft delete.
        logger.exception("Could not purge deleted knowledge document=%s", document_id)
    return {"code": 0, "data": None, "message": "ok"}
