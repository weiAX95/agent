# 模块职责：定义分块目录、索引任务和审计三张新增表；任务状态与租约写入数据库以支持跨进程恢复。
"""Additive tables for recoverable revisions, lexical retrieval and management audit."""
# 导入 uuid，用于验证 UUID 或生成稳定/随机标识。
import uuid
# 从 datetime 导入 datetime，供下方统一使用带时区的时间。
from datetime import datetime
# 从 sqlalchemy 导入 DateTime, Index, Integer, JSON, String, Text，供下方构造数据库字段与参数化查询。
from sqlalchemy import DateTime, Index, Integer, JSON, String, Text
# 从 sqlalchemy.dialects.postgresql 导入 JSONB, UUID，供下方使用 PostgreSQL 特定类型。
from sqlalchemy.dialects.postgresql import JSONB, UUID
# 从 sqlalchemy.orm 导入 Mapped, mapped_column，供下方声明 ORM 字段或管理短会话。
from sqlalchemy.orm import Mapped, mapped_column
# 从 sqlalchemy.sql 导入 func，供下方相关数据结构与业务步骤。
from sqlalchemy.sql import func
# 从 api.base 导入 Base，供下方相关数据结构与业务步骤。
from api.base import Base

# PostgreSQL 使用可建 GIN 索引的 JSONB；离线 SQLite 测试回退为通用 JSON 类型。
JSON_DATA = JSON().with_variant(JSONB(), 'postgresql')


# 定义类型：SQL 分块目录模型，支持有效版本查询和关键词 GIN 索引。
class KnowledgeChunkRecord(Base):
    # 指定 ORM 表名，使该模型映射到持久数据库中的对应表。
    __tablename__ = 'knowledge_chunk_records'
    # 声明辅助索引，分别支持文档版本定位和关键词 GIN 查询。
    __table_args__ = (
        # 创建按文档、revision 和块序号查找的复合索引，供版本定位与相邻块读取。
        Index('ix_knowledge_chunks_revision', 'document_id', 'revision', 'chunk_index'),
        # 创建关键词 JSONB 的 GIN 倒排索引，加速任意 token 命中筛选。
        Index('ix_knowledge_chunks_tokens', 'search_tokens', postgresql_using='gin'),
    # 结束当前参数列表或结构化数据，后续语句继续处理这一结果。
    )
    # 声明「记录唯一 ID」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # 声明「逻辑文档 ID」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # 声明「不可变文档版本」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    revision: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # 声明「分块顺序号」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 声明「页码（无页码模型时为 0）」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    # 声明「章节路径」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    section_path: Mapped[str] = mapped_column(Text, default='', nullable=False)
    # 声明「片段原文」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # 声明「关键词 token 数组」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    search_tokens: Mapped[list] = mapped_column(JSON_DATA, nullable=False)


# 定义类型：可恢复的索引任务模型，持久保存状态、重试次数、版本和租约。
class KnowledgeIndexJob(Base):
    # 指定 ORM 表名，使该模型映射到持久数据库中的对应表。
    __tablename__ = 'knowledge_index_jobs'
    # 声明「记录唯一 ID」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 声明「逻辑文档 ID」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # 声明「不可变文档版本」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    revision: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    # 声明「替换操作预期的旧版本」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    base_revision: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # 声明「执行操作的用户 UUID」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # 声明「原文件名称」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # 声明「原文件 SHA-256 摘要」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # 声明「任务/处理状态」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    status: Mapped[str] = mapped_column(String(20), default='queued', nullable=False, index=True)
    # 声明「任务已执行次数」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 声明「当前执行者的租约令牌」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    # 声明「租约到期时间」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 声明「最近失败原因」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    error: Mapped[str | None] = mapped_column(Text)
    # 声明「解析与模型版本快照」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    index_config: Mapped[dict] = mapped_column(JSON_DATA, nullable=False)
    # 声明「创建时间」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # 声明「最近更新时间」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# 定义类型：文档管理审计模型，保存执行人、动作、目标及结构化详情。
class KnowledgeAuditEvent(Base):
    # 指定 ORM 表名，使该模型映射到持久数据库中的对应表。
    __tablename__ = 'knowledge_audit_events'
    # 声明「记录唯一 ID」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 声明「执行操作的用户 UUID」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # 声明「逻辑文档 ID」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # 声明「审计动作名称」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    # 声明「结构化审计信息」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    details: Mapped[dict] = mapped_column(JSON_DATA, default=dict, nullable=False)
    # 声明「创建时间」数据库列；类型、可空性、索引或默认值由 mapped_column 指定。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
