"""Short business transactions; caller holds the session execution lock."""

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import anyio
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.agent.checkpoint import checkpoint_lock_id, make_thread_id

from api.models import ChatLog, ChatMessage, ChatRunEvent, ChatSession


async def create_session(db: AsyncSession, owner: uuid.UUID, title: str) -> ChatSession:
    session_id = uuid.uuid4()
    session = ChatSession(
        id=session_id,
        owner_id=owner,
        title=title.strip() or "新会话",
        checkpoint_thread_id=make_thread_id(str(owner), str(session_id)),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def owned_session(db: AsyncSession, owner: uuid.UUID, session_id: uuid.UUID):
    session = await db.scalar(
        select(ChatSession)
        .where(
            ChatSession.id == session_id,
            ChatSession.owner_id == owner,
            ChatSession.deleted_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    if session is None:
        raise HTTPException(404, "会话不存在或不可访问，请先创建会话")
    return session


@asynccontextmanager
async def session_execution_lock(pool, owner: uuid.UUID, session_id: uuid.UUID):
    """Session-level lock survives message commits; never return a locked connection."""
    key = checkpoint_lock_id(str(owner), str(session_id))
    async with pool.connection() as connection:
        try:
            with anyio.CancelScope(shield=True):
                cursor = await connection.execute(
                    "SELECT pg_try_advisory_lock(%s) AS acquired", (key,)
                )
                row = await cursor.fetchone()
        except BaseException:
            # Cancellation may occur after PostgreSQL acquired the lock but before fetch.
            with anyio.CancelScope(shield=True):
                await connection.close()
            raise
        if not row["acquired"]:
            raise HTTPException(409, "该会话正在生成，请结束后再发送")
        try:
            yield
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
                except BaseException:
                    # A broken connection must not carry a session lock back to the pool.
                    await connection.close()
                    raise


@dataclass
class ChatTurn:
    db: AsyncSession
    session: ChatSession
    assistant: ChatMessage
    user_content: str
    replayed: bool = False
    assistant_id: uuid.UUID = field(init=False)
    session_id: uuid.UUID = field(init=False)
    owner_id: uuid.UUID = field(init=False)
    request_id: uuid.UUID = field(init=False)
    event_sequence: int = 0

    def __post_init__(self):
        # Retain identifiers even when SQLAlchemy expires ORM objects after rollback.
        self.assistant_id = self.assistant.id
        self.session_id = self.session.id
        self.owner_id = self.session.owner_id
        self.request_id = self.assistant.request_id

    @property
    def log_id(self):
        return (
            self.assistant_id
            if self.assistant.status in ("completed", "stopped")
            else None
        )

    async def append_event(self, stage: str, status: str, summary: str, sources=None):
        if self.replayed:
            raise ValueError("Cannot append to a replayed turn")
        self.event_sequence += 1
        event = ChatRunEvent(
            id=uuid.uuid4(), message_id=self.assistant_id,
            sequence=self.event_sequence, stage=stage, status=status,
            summary=summary[:240], sources=(sources or [])[:5],
        )
        try:
            self.db.add(event)
            await self.db.commit()
            await self.db.refresh(event)
        except BaseException:
            await self.db.rollback()
            raise
        return {
            "id": str(event.id), "message_id": str(self.assistant_id),
            "sequence": event.sequence, "stage": event.stage,
            "status": event.status, "summary": event.summary,
            "sources": event.sources, "created_at": event.created_at.isoformat(),
        }

    async def save_partial(self, content: str):
        await self.db.execute(
            update(ChatMessage)
            .where(
                ChatMessage.id == self.assistant_id,
                ChatMessage.status == "streaming",
            )
            .values(content=content, updated_at=datetime.now(UTC))
        )
        await self.db.commit()

    async def finish(self, status: str, content: str, error_code: str | None = None,
                     memory_status: str | None = None):
        if status not in {"completed", "stopped", "failed", "interrupted"}:
            raise ValueError("Invalid terminal message status")
        if memory_status not in {None, "saved", "unchanged", "skipped", "failed", "unknown"}:
            raise ValueError("Invalid memory status")
        now = datetime.now(UTC)
        try:
            result = await self.db.execute(
                update(ChatMessage)
                .where(
                    ChatMessage.id == self.assistant_id,
                    ChatMessage.status == "streaming",
                )
                .values(
                    content=content,
                    status=status,
                    error_code=error_code,
                    memory_status=memory_status,
                    updated_at=now,
                    finished_at=now,
                )
            )
            if result.rowcount:
                await self.db.execute(
                    update(ChatSession)
                    .where(
                        ChatSession.id == self.session_id,
                    )
                    .values(last_message_at=now, updated_at=now)
                )
                if status in {"completed", "stopped"}:
                    answer = content + (
                        "\n\n[本轮已停止生成]" if status == "stopped" else ""
                    )
                    self.db.add(
                        ChatLog(
                            id=self.assistant_id,
                            user_id=str(self.owner_id),
                            session_id=str(self.session_id),
                            message=self.user_content,
                            answer=answer,
                        )
                    )
            await self.db.commit()
            if result.rowcount:
                self.assistant.memory_status = memory_status
        except BaseException:
            await self.db.rollback()
            raise
        return self.assistant_id if status in {"completed", "stopped"} else None

    async def interrupt_if_streaming(self):
        await self.db.rollback()
        await self.db.execute(
            update(ChatMessage)
            .where(
                ChatMessage.id == self.assistant_id,
                ChatMessage.status == "streaming",
            )
            .values(
                status="interrupted",
                error_code="CHECKPOINT_RECOVERY_REQUIRED",
                updated_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        )
        await self.db.commit()


async def begin_turn(db, owner, session_id, request_id, message):
    """Reserve a pair or replay a terminal result. Must run under execution lock."""
    session = await owned_session(db, owner, session_id)
    if session.checkpoint_thread_id != make_thread_id(str(owner), str(session_id)):
        raise HTTPException(409, "此会话需要先完成旧线程映射迁移")
    rows = (
        await db.scalars(
            select(ChatMessage).where(
                ChatMessage.session_id == session_id,
                ChatMessage.request_id == request_id,
            )
        )
    ).all()
    if rows:
        by_role = {row.role: row for row in rows}
        if set(by_role) != {"user", "assistant"}:
            raise HTTPException(409, "会话消息不完整，需要恢复后再使用")
        if by_role["user"].content != message:
            raise HTTPException(409, "同一 request_id 不能使用不同消息")
        assistant = by_role["assistant"]
        if assistant.status != "streaming":
            return ChatTurn(db, session, assistant, message, replayed=True)

    # Holding the advisory lock proves no other conforming worker is executing.
    # An orphan might already have updated its checkpoint; never rerun blindly.
    stale = await db.scalar(
        select(ChatMessage)
        .where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
            (ChatMessage.status == "streaming")
            | (
                (ChatMessage.status == "interrupted")
                & (ChatMessage.error_code == "CHECKPOINT_RECOVERY_REQUIRED")
            ),
        )
        .limit(1)
    )
    if stale is not None:
        if stale.status == "streaming":
            stale.status = "interrupted"
            stale.error_code = "CHECKPOINT_RECOVERY_REQUIRED"
            stale.finished_at = stale.updated_at = datetime.now(UTC)
            await db.commit()
        raise HTTPException(409, "上次执行异常中断，需要核对上下文；请使用新会话")

    now = datetime.now(UTC)
    seq = session.next_message_seq
    user = ChatMessage(
        id=uuid.uuid4(),
        session_id=session_id,
        request_id=request_id,
        seq=seq,
        role="user",
        content=message,
        status="completed",
        finished_at=now,
    )
    assistant = ChatMessage(
        id=uuid.uuid4(),
        session_id=session_id,
        request_id=request_id,
        seq=seq + 1,
        role="assistant",
        content="",
        status="streaming",
    )
    session.next_message_seq += 2
    if session.last_message_at is None and session.title == "新会话":
        session.title = message.strip()[:200]
    session.last_message_at = session.updated_at = now
    db.add_all([user, assistant])
    await db.commit()
    return ChatTurn(db, session, assistant, message)
