import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey,
    Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from api.base import Base


def gen_uuid():
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=gen_uuid
    )
    username: Mapped[str] = mapped_column(
        String, unique=True, nullable=False, index=True
    )
    email: Mapped[str | None] = mapped_column(String, unique=True)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        UniqueConstraint("checkpoint_thread_id", name="uq_chat_sessions_thread"),
        UniqueConstraint("owner_id", "legacy_session_id", name="uq_chat_sessions_legacy"),
        CheckConstraint("next_message_seq >= 1", name="ck_chat_sessions_seq"),
        CheckConstraint("length(trim(title)) > 0", name="ck_chat_sessions_title"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False, server_default="新会话")
    checkpoint_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    legacy_session_id: Mapped[str | None] = mapped_column(Text)
    next_message_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index(
    "ix_chat_sessions_owner_activity", ChatSession.owner_id,
    ChatSession.last_message_at.desc(), ChatSession.id.desc(),
    postgresql_where=text("deleted_at IS NULL AND last_message_at IS NOT NULL"),
)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_chat_messages_seq"),
        UniqueConstraint("session_id", "request_id", "role", name="uq_chat_messages_request_role"),
        CheckConstraint("seq >= 1", name="ck_chat_messages_seq"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_messages_role"),
        CheckConstraint("status IN ('streaming','completed','stopped','failed','interrupted')", name="ck_chat_messages_status"),
        CheckConstraint("role <> 'user' OR status = 'completed'", name="ck_chat_messages_user_status"),
        CheckConstraint(
            "(status = 'streaming' AND finished_at IS NULL) OR "
            "(status <> 'streaming' AND finished_at IS NOT NULL)",
            name="ck_chat_messages_finished",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_sessions.id", ondelete="RESTRICT"), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    model_profile: Mapped[str] = mapped_column(String(32), nullable=False, server_default="default")
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    memory_status: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatRunEvent(Base):
    __tablename__ = "chat_run_events"
    __table_args__ = (
        UniqueConstraint("message_id", "sequence", name="uq_chat_run_events_order"),
        CheckConstraint("sequence >= 1", name="ck_chat_run_events_sequence"),
        CheckConstraint("stage IN ('research','answer','memory')", name="ck_chat_run_events_stage"),
        CheckConstraint("status IN ('started','completed','failed','stopped','skipped')", name="ck_chat_run_events_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(String(240), nullable=False)
    sources: Mapped[list] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AgentRun(Base):
    """持久化执行事实；消息表继续负责界面展示与原有 SSE 协议。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("session_id", "request_id", name="uq_agent_runs_request"),
        UniqueConstraint("assistant_message_id", name="uq_agent_runs_assistant"),
        CheckConstraint("status IN ('created','running','succeeded','failed','cancelled','timed_out')", name="ck_agent_runs_status"),
        CheckConstraint("max_steps > 0 AND steps >= 0 AND steps <= max_steps", name="ck_agent_runs_steps"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_sessions.id", ondelete="RESTRICT"), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assistant_message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_messages.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="created")
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    recovery_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRunReconciliation(Base):
    """人工裁决的不可重复审计记录；不保存原始聊天或工具输出。"""

    __tablename__ = "agent_run_reconciliations"
    __table_args__ = (
        CheckConstraint("external_effect_status IN ('none','confirmed')",
                        name="ck_run_reconciliation_effect_status"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), primary_key=True,
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    checkpoint_id: Mapped[str | None] = mapped_column(Text)
    external_effect_status: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class GatewayPriceVersion(Base):
    """按版本保存模型费率；历史事件始终引用原价目表。"""

    __tablename__ = "gateway_price_versions"
    __table_args__ = (
        CheckConstraint("input_per_million >= 0 AND output_per_million >= 0 AND cached_input_per_million >= 0",
                        name="ck_gateway_price_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    input_per_million: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    output_per_million: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    cached_input_per_million: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class GatewayModelCall(Base):
    """每次物理上游尝试独占一个 call_id；重试必须新建一行。"""

    __tablename__ = "gateway_model_calls"
    __table_args__ = (
        CheckConstraint("status IN ('started','succeeded','failed','cancelled','timed_out','unknown')",
                        name="ck_gateway_call_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="started")
    price_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("gateway_price_versions.id", ondelete="RESTRICT"))
    trace_id: Mapped[str | None] = mapped_column(String(128))
    upstream_request_id: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GatewayUsageEvent(Base):
    """只插入的供应商用量回执；缺失用量与费用保持 NULL。"""

    __tablename__ = "gateway_usage_events"
    __table_args__ = (
        UniqueConstraint("call_id", "event_key", name="uq_gateway_usage_receipt"),
        CheckConstraint("status IN ('succeeded','failed','cancelled','timed_out','unknown')",
                        name="ck_gateway_usage_status"),
        CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="ck_gateway_input_tokens"),
        CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="ck_gateway_output_tokens"),
        CheckConstraint("cached_input_tokens IS NULL OR cached_input_tokens >= 0",
                        name="ck_gateway_cached_tokens"),
        CheckConstraint("input_tokens IS NULL OR cached_input_tokens IS NULL OR cached_input_tokens <= input_tokens",
                        name="ck_gateway_cached_within_input"),
        CheckConstraint("cost IS NULL OR (cost >= 0 AND price_version_id IS NOT NULL)",
                        name="ck_gateway_cost_price"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gateway_model_calls.id", ondelete="RESTRICT"), nullable=False)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cached_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    price_version_id: Mapped[str | None] = mapped_column(ForeignKey("gateway_price_versions.id", ondelete="RESTRICT"))
    currency: Mapped[str | None] = mapped_column(String(3))
    cost: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    is_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class KnowledgeDocument(Base):
    """Catalog for the shared knowledge base; Milvus stores its active chunks."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="ck_knowledge_documents_size"),
        CheckConstraint("chunk_count > 0", name="ck_knowledge_documents_chunks"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    revision: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index(
    "ix_knowledge_documents_created", KnowledgeDocument.created_at.desc(),
    KnowledgeDocument.id.desc(),
    postgresql_where=text("deleted_at IS NULL"),
)


class ChatLog(Base):
    __tablename__ = "chat_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatFeedback(Base):
    """A user's report of one completed answer; raw chat text stays in ChatLog."""

    __tablename__ = "chat_feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    chat_log_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_logs.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LearningPath(Base):
    __tablename__ = "learning_paths"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=gen_uuid
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String, default="active"
    )  # active | completed | archived
    expected_weeks: Mapped[int | None] = mapped_column(Integer)
    owner_id: Mapped[str | None] = mapped_column(
        String
    )  # 本地单用户先不设外键，以后接auth再改
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    topics: Mapped[list["Topic"]] = relationship(
        back_populates="path", cascade="all, delete-orphan"
    )


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=gen_uuid
    )
    path_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("learning_paths.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    path: Mapped["LearningPath"] = relationship(back_populates="topics")
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="topic", cascade="all, delete-orphan"
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=gen_uuid
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topics.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String, default="todo"
    )  # todo | in_progress | done
    priority: Mapped[str] = mapped_column(String, default="medium")
    due_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    topic: Mapped["Topic"] = relationship(back_populates="tasks")

    VALID_TRANSITIONS = {
        "todo": {"in_progress"},
        "in_progress": {"done", "todo"},
        "done": {"in_progress"},
    }

# Register additive knowledge tables with the application's shared metadata.
from api.knowledge_models import KnowledgeAuditEvent, KnowledgeChunkRecord, KnowledgeIndexJob
