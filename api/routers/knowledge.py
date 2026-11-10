# 模块职责：提供已认证的知识库管理 API，串联文件归档、幂等任务、同步等待、后台查询和版本冲突保护。
"""Authenticated management of a knowledge base shared by all signed-in users."""

# 导入 logging，用于记录模块运行状态与可排查的故障信息。
import logging
# 导入 hashlib，用于计算文件、配置与模型内容摘要。
import hashlib
# 导入 uuid，用于验证 UUID 或生成稳定/随机标识。
import uuid
# 从 datetime 导入 UTC, datetime，供下方统一使用带时区的时间。
from datetime import UTC, datetime

# 从 fastapi 导入 APIRouter, Depends, File, HTTPException, Query, UploadFile，供下方相关数据结构与业务步骤。
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
# 从 fastapi.responses 导入 JSONResponse，供下方相关数据结构与业务步骤。
from fastapi.responses import JSONResponse
# 从 sqlalchemy 导入 and_, or_, select，供下方构造数据库字段与参数化查询。
from sqlalchemy import and_, or_, select
# 从 sqlalchemy.ext.asyncio 导入 AsyncSession，供下方相关数据结构与业务步骤。
from sqlalchemy.ext.asyncio import AsyncSession
# 从 starlette.concurrency 导入 run_in_threadpool，供下方相关数据结构与业务步骤。
from starlette.concurrency import run_in_threadpool

# 从 api.auth 导入 get_current_user，供下方相关数据结构与业务步骤。
from api.auth import get_current_user
# 从 api.db 导入 get_db，供下方相关数据结构与业务步骤。
from api.db import get_db
# 从 api.models 导入 KnowledgeDocument，供下方相关数据结构与业务步骤。
from api.models import KnowledgeDocument
# 从 api.knowledge_models 导入 KnowledgeAuditEvent, KnowledgeIndexJob，供下方相关数据结构与业务步骤。
from api.knowledge_models import KnowledgeAuditEvent, KnowledgeIndexJob
# 从 api.knowledge_policy 导入 require_knowledge_manager，供下方相关数据结构与业务步骤。
from api.knowledge_policy import require_knowledge_manager
# 从 src.agent.knowledge_jobs 导入 make_job, run_job, retry_job，供下方相关数据结构与业务步骤。
from src.agent.knowledge_jobs import make_job, run_job, retry_job
# 从 src.agent.knowledge_database 导入 knowledge_engine，供下方相关数据结构与业务步骤。
from src.agent.knowledge_database import knowledge_engine
# 从 src.agent.knowledge_ingest 导入 MAX_UPLOAD_BYTES, CONTENT_TYPES, _filename, DocumentValidationError, PreparedDocument, prepare_document，供下方相关数据结构与业务步骤。
from src.agent.knowledge_ingest import (
    # 导入上传边界、格式映射、领域错误、数据类与解析函数，供 API 和 worker 共用约束。
    MAX_UPLOAD_BYTES, CONTENT_TYPES, _filename, DocumentValidationError, PreparedDocument, prepare_document,
# 结束当前参数列表或结构化数据，后续语句继续处理这一结果。
)
# 从 src.agent.knowledge_store 导入 delete_document_chunks, index_document，供下方相关数据结构与业务步骤。
from src.agent.knowledge_store import delete_document_chunks, index_document

# 准备「知识库文档管理路由集合」：创建带统一路径前缀的 API 路由集合。
router = APIRouter(prefix="/api/knowledge/documents", tags=["knowledge"])
# 准备「当前模块日志记录器」：取得当前模块的日志对象。
logger = logging.getLogger(__name__)


# 定义函数：将已认证用户标识转换成 UUID，无法转换时返回 401。
def _actor(current_user: dict) -> uuid.UUID:
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 返回标准 UUID；接口不会直接使用未校验的用户字符串。
        return uuid.UUID(current_user["user_id"])
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except (KeyError, TypeError, ValueError) as exc:
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(401, "用户标识无效") from exc


# 定义函数：把文档 ORM 对象转换成可 JSON 序列化的接口元数据。
def _data(document: KnowledgeDocument) -> dict:
    # 返回仅包含可公开文档元数据的字典，UUID/时间已转换成 JSON 友好形式。
    return {
        # 写入「记录唯一 ID」字段，与同一条记录的其他字段共同组成结果。
        "id": str(document.id),
        # 写入「不可变文档版本」字段，与同一条记录的其他字段共同组成结果。
        "revision": str(document.revision),
        # 写入「原文件 SHA-256 摘要」字段，与同一条记录的其他字段共同组成结果。
        "sha256": document.sha256,
        # 写入「原文件名称」字段，与同一条记录的其他字段共同组成结果。
        "filename": document.filename,
        # 写入「文件 MIME 类型」字段，与同一条记录的其他字段共同组成结果。
        "content_type": document.content_type,
        # 写入「原文件字节数」字段，与同一条记录的其他字段共同组成结果。
        "size_bytes": document.size_bytes,
        # 写入「分块数量」字段，与同一条记录的其他字段共同组成结果。
        "chunk_count": document.chunk_count,
        # 写入「上传者 UUID」字段，与同一条记录的其他字段共同组成结果。
        "uploaded_by": str(document.uploaded_by),
        # 写入「创建时间」字段，与同一条记录的其他字段共同组成结果。
        "created_at": document.created_at.isoformat(),
        # 写入「最近更新时间」字段，与同一条记录的其他字段共同组成结果。
        "updated_at": document.updated_at.isoformat(),
    # 结束当前参数列表或结构化数据，后续语句继续处理这一结果。
    }


# 定义函数：有界读取并关闭上传文件；可只构造轻量元数据，把实际解析留给 worker。
async def _prepare_upload(file: UploadFile, defer=False) -> PreparedDocument:
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 准备「原始文件字节」：等待异步操作完成，再继续当前请求。
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
    # 无论成功、失败或提前返回，都执行下方资源清理/统计逻辑。
    finally:
        # 等待当前数据库/文件异步操作完成后，再执行下一条依赖它的语句。
        await file.close()
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
        if defer:
            # 准备「规范化名称」：移除上传路径并校验文件名长度、控制字符和受支持后缀。
            name = _filename(file.filename)
            # 拒绝空文件及超过 10 MiB 的原始内容，在进一步解析前建立资源边界。
            if not raw or len(raw) > MAX_UPLOAD_BYTES:
                # 拒绝不满足文件格式、大小或解析约束的文档，并向上层提供可解释原因。
                raise DocumentValidationError("文件为空或超过 10 MB")
            # 从 pathlib 导入 PurePath，供下方按路径对象处理文件。
            from pathlib import PurePath
            # 把解析或轻量准备工作放在线程池执行，避免阻塞异步 API 的事件循环。
            return PreparedDocument(name, CONTENT_TYPES[PurePath(name).suffix.lower()],
                # 记录原文件大小和摘要，分块先留空；后台 worker 后续再执行真正解析。
                len(raw), hashlib.sha256(raw).hexdigest(), [], raw)
        # 把解析或轻量准备工作放在线程池执行，避免阻塞异步 API 的事件循环。
        return await run_in_threadpool(prepare_document, file.filename, raw)
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except DocumentValidationError as exc:
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(422, str(exc)) from exc


# 定义函数：输出可公开查询的任务状态与配置，隐藏内部错误详情及模型绝对路径。
def _job_data(job):
    # 从 pathlib 导入 Path，供下方按路径对象处理文件。
    from pathlib import Path
    # 返回任务状态的公开视图；模型目录只暴露名称，不暴露本机绝对路径。
    return {"id": str(job.id), "document_id": str(job.document_id), "revision": str(job.revision),
            # 写入「任务/处理状态」字段，与同一条记录的其他字段共同组成结果。
            "status": job.status, "attempts": job.attempts,
            # 写入「最近失败原因」字段，与同一条记录的其他字段共同组成结果。
            "error": "索引失败，请查看服务日志或重试" if job.error else None,
            # 写入「解析与模型版本快照」字段，与同一条记录的其他字段共同组成结果。
            "index_config": {k:v for k,v in job.index_config.items() if k != 'embedding'} | {
                # 写入「embedding」字段，与同一条记录的其他字段共同组成结果。
                'embedding':Path(job.index_config.get('embedding','')).name}}


# 定义函数：处理请求幂等、原文件归档与任务提交；按参数同步等待或返回真实后台状态。
async def _submit(db, prepared, actor, *, document_id=None, base_revision=None, request_id=None, background=False):
    # 定义函数：确认重试请求的操作者、内容、文件名、操作类型及目标都与原任务一致。
    def matches(existing):
        # 所有关键字段都一致才把本次调用视为同一请求的安全重试。
        return (existing.actor_id == actor and existing.sha256 == prepared.sha256 and
                # 同一幂等 ID 的文件名也必须一致，不能只比较内容哈希。
                existing.filename == prepared.filename and
                # 创建和替换属于不同操作类型；幂等 ID 不能跨类型复用。
                ((existing.base_revision is None) == (base_revision is None)) and
                # 若本次指定目标文档，必须与原任务目标一致，避免把另一文档的任务当作重试。
                (document_id is None or existing.document_id == document_id))

    # 提供幂等请求 ID 时先查已存在任务，避免网络重试重复归档和入队。
    if request_id:
        # 准备「数据库中已存在的文档或任务」：等待异步操作完成，再继续当前请求。
        existing = await db.get(KnowledgeIndexJob, request_id)
        # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
        if existing is not None:
            # 复用请求 ID 必须匹配全部关键字段；相同内容但不同文件名或目标也视为冲突。
            if not matches(existing):
                # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
                raise HTTPException(409, "request_id 已被其他操作使用")
            # 准备「索引任务对象」：使用数据库中已存在的文档或任务。
            job = existing
        # 进入前一条件未满足时的分支，采用对应的回退处理。
        else:
            # 准备「索引任务对象」：用 None 表示此处尚无值或已清空状态。
            job = None
    # 进入前一条件未满足时的分支，采用对应的回退处理。
    else:
        # 准备「索引任务对象」：用 None 表示此处尚无值或已清空状态。
        job = None
    # 没有找到可复用/领取的任务时进入相应分支；提交接口会创建，worker 则返回空任务。
    if job is None:
        # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
        try:
            # 准备「索引任务对象」：等待异步操作完成，再继续当前请求。
            job = await run_in_threadpool(make_job, prepared=prepared, actor_id=actor,
                # 设置「逻辑文档 ID」参数，使当前调用使用明确的配置或元数据。
                document_id=document_id, base_revision=base_revision, request_id=request_id)
        # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
        except ValueError as exc:
            # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
            raise HTTPException(409, "相同请求标识不可用于不同文件") from exc
        # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
        except OSError as exc:
            # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
            raise HTTPException(503, "原文件归档暂不可用") from exc
        # 在可能失败的提交前保存普通 UUID 值，回滚后不访问会过期的 ORM 属性。
        job_identifier = job.id
        # 把该项加入去重集合或当前数据库事务。
        db.add(job)
        # 把该项加入去重集合或当前数据库事务。
        db.add(KnowledgeAuditEvent(actor_id=actor, document_id=job.document_id,
            # 设置「审计动作名称」参数，使当前调用使用明确的配置或元数据。
            action="index_queued", details={"job_id": str(job.id), "revision": str(job.revision)}))
        # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
        try:
            # 提交当前数据库事务，使任务状态或文档变更正式生效，同时释放该事务持有的行锁。
            await db.commit()
        # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
        except Exception:
            # 回滚并释放当前事务；可能是出错恢复，也可能是主动避免在文件解析期间占用事务。
            await db.rollback()
            # A concurrent identical request may already have committed the task.
            # 准备「数据库中已存在的文档或任务」：等待异步操作完成，再继续当前请求。
            existing = await db.get(KnowledgeIndexJob, job_identifier)
            # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
            if existing is None:
                # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
                raise HTTPException(503, "任务状态待核对，请使用相同 request_id 重试")
            # 复用请求 ID 必须匹配全部关键字段；相同内容但不同文件名或目标也视为冲突。
            if not matches(existing):
                # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
                raise HTTPException(409, "request_id 已被其他操作使用")
            # 准备「索引任务对象」：使用数据库中已存在的文档或任务。
            job = existing
    # 默认兼容同步接口：只有新排队任务才在线程池执行，后台模式不阻塞等待。
    if not background and job.status == "queued":
        # 把同步归档/索引操作放到线程池等待，避免阻塞异步请求的事件循环。
        await run_in_threadpool(run_job, knowledge_engine(), job.id)
        # 从数据库刷新任务状态，读取同步 worker 实际提交的结果，而不是沿用入队时的旧对象值。
        await db.refresh(job)
    # 只有持久任务真正成功发布后，才允许返回成功文档元数据。
    if job.status == "succeeded":
        # 重新读取任务对应文档的最新状态，避免会话缓存仍保留发布前旧值。
        document = await db.get(KnowledgeDocument, job.document_id, populate_existing=True)
        # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
        if document is None or document.deleted_at is not None:
            # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
            raise HTTPException(409, "文档随后已被删除")
        # 即使这个任务曾成功，文档随后被替换也不能把当前版本冒充为本请求生成的结果。
        if document.revision != job.revision:
            # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
            raise HTTPException(409, "该任务已完成，但文档随后已被其他版本替换")
        # 根据任务当前状态返回成功文档、后台处理状态或相应 HTTP 错误。
        return {"code": 0, "data": _data(document), "message": "ok"}
    # 任务与其他文档操作冲突时返回 409，要求客户端刷新，不自动覆盖。
    if job.status == "conflict":
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(409, "文档已被其他操作修改，请刷新后重试")
    # 失败任务不能返回成功；文件错误与索引基础设施错误在下方分别处理。
    if job.status == "failed":
        # 根据持久任务状态选择操作，不能把排队、执行中、冲突和成功混为一谈。
        if job.error and job.error.startswith('DocumentValidationError: '):
            # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
            raise HTTPException(422, job.error.split(': ',1)[1])
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(503, {"message":"索引失败，可查询任务状态后重试", "job_id":str(job.id)})
    # 任务尚未完成时返回 202 和真实状态，不能把 queued/running 当作文档已索引成功。
    return JSONResponse(status_code=202, content={"code":0, "data":_job_data(job), "message":"queued"})


# 注册知识库 GET 路由；具体路径由路由前缀和下方处理函数的参数共同组成。
@router.get("")
# 定义函数：按创建时间与 ID 组成的游标分页列出仍有效的共享文档。
async def list_documents(
    # 声明「结果/候选数量上限」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    limit: int = Query(default=50, ge=1, le=100),
    # 声明「上一页最后一条文档的创建时间」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    before_created_at: datetime | None = None,
    # 声明「上一页最后一条文档的 UUID」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    before_id: uuid.UUID | None = None,
    # 声明「JWT 认证依赖返回的当前用户」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    current_user: dict = Depends(get_current_user),
    # 声明「请求级异步数据库会话」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    db: AsyncSession = Depends(get_db),
# 结束函数参数与返回类型声明，下面进入处理正文。
):
    # 将已认证用户标识转换成 UUID，无法转换时返回 401。
    _actor(current_user)
    # 游标时间与 ID 必须成对提供，单字段游标可能造成重复或漏页。
    if (before_created_at is None) != (before_id is None):
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(422, "分页游标不完整")
    # 分页时间必须携带时区，才能与数据库时间做一致比较。
    if before_created_at is not None and before_created_at.tzinfo is None:
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(422, "分页时间必须包含时区")
    # 准备「检索问题/当前 SQL 查询」：为 SQL 语句添加过滤条件。
    query = select(KnowledgeDocument).where(KnowledgeDocument.deleted_at.is_(None))
    # 只有提供完整有效的分页游标时才增加对应过滤条件。
    if before_created_at is not None:
        # 准备「检索问题/当前 SQL 查询」：为 SQL 语句添加过滤条件。
        query = query.where(or_(
            # 分页优先取创建时间早于游标时间的记录。
            KnowledgeDocument.created_at < before_created_at,
            # 对于创建时间恰好相同的记录，再使用 UUID 作稳定的第二排序键。
            and_(
                # 仅在创建时间相同的这组记录里应用下行的 ID 边界。
                KnowledgeDocument.created_at == before_created_at,
                # 同时间记录只取 ID 小于游标的部分，与倒序排序保持一致。
                KnowledgeDocument.id < before_id,
            # 结束当前SQL 条件/对象构造，后续语句继续处理这一结果。
            ),
        # 结束当前SQL 条件/对象构造，后续语句继续处理这一结果。
        ))
    # 准备「数据库行或文档表格行」：取出本次有界查询的全部返回行。
    rows = (await db.scalars(query.order_by(
        # 同时按创建时间和 ID 倒序，保证相同时间戳下的分页也有稳定顺序。
        KnowledgeDocument.created_at.desc(), KnowledgeDocument.id.desc(),
    # 结束当前SQL 条件/对象构造，后续语句继续处理这一结果。
    ).limit(limit + 1))).all()
    # 只把请求数量以内的文档作为本页结果；额外读取的一条仅用于判断是否还有下一页。
    page = rows[:limit]
    # 准备「用于生成下一页游标的最后一行」：根据条件选择当前值或回退值。
    last = page[-1] if len(rows) > limit else None
    # 返回本页文档和可选下一页游标，不一次性读取整个共享目录。
    return {"code": 0, "data": {
        # 写入「按文档和片段 ID 去重的候选映射」字段，与同一条记录的其他字段共同组成结果。
        "items": [_data(row) for row in page],
        # 写入「next_before」字段，与同一条记录的其他字段共同组成结果。
        "next_before": {
            # 写入「创建时间」字段，与同一条记录的其他字段共同组成结果。
            "created_at": last.created_at.isoformat(), "id": str(last.id),
        # 只有确实存在下一页时才输出游标，否则明确返回 None。
        } if last else None,
    # 结束当前SQL 条件/对象构造，后续语句继续处理这一结果。
    }, "message": "ok"}


# 注册知识库 POST 路由；具体路径由路由前缀和下方处理函数的参数共同组成。
@router.post("", status_code=201)
# 定义函数：检查管理权限后提交新文档，保留默认同步成功响应并支持显式后台模式。
async def upload_document(
    # 声明「上传文件流」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    file: UploadFile = File(...), background: bool = False, request_id: uuid.UUID | None = None,
    # 声明「JWT 认证依赖返回的当前用户」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db),
# 结束函数参数与返回类型声明，下面进入处理正文。
):
    # 准备「通过校验的用户 UUID」：验证用户 UUID 与服务端管理策略；shared 放行，admin 检查管理员名单。
    actor = require_knowledge_manager(current_user)
    # 准备「解析完成的文档」：等待异步操作完成，再继续当前请求。
    prepared = await _prepare_upload(file, defer=True)
    # 通过统一提交逻辑处理新上传，幂等与后台行为集中在 _submit 中。
    return await _submit(db, prepared, actor, request_id=request_id, background=background)


# 注册知识库 PUT 路由；具体路径由路由前缀和下方处理函数的参数共同组成。
@router.put("/{document_id}")
# 定义函数：读取当前版本作为替换基线，发布时再次核对，避免覆盖他人新版本。
async def replace_document(
    # 声明「逻辑文档 ID」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    document_id: uuid.UUID, file: UploadFile = File(...), background: bool = False,
    # 声明「可选幂等请求 UUID」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    request_id: uuid.UUID | None = None,
    # 声明「JWT 认证依赖返回的当前用户」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    current_user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db),
# 结束函数参数与返回类型声明，下面进入处理正文。
):
    # 准备「通过校验的用户 UUID」：验证用户 UUID 与服务端管理策略；shared 放行，admin 检查管理员名单。
    actor = require_knowledge_manager(current_user)
    # 准备「数据库中已存在的文档或任务」：等待异步操作完成，再继续当前请求。
    existing = await db.get(KnowledgeDocument, document_id)
    # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
    if existing is None or existing.deleted_at is not None:
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(404, "文档不存在")
    # 准备「开始替换时读取的有效 revision」：读取 不可变文档版本。
    old_revision = existing.revision
    # 回滚并释放当前事务；可能是出错恢复，也可能是主动避免在文件解析期间占用事务。
    await db.rollback()
    # 准备「解析完成的文档」：等待异步操作完成，再继续当前请求。
    prepared = await _prepare_upload(file, defer=True)
    # 提交带旧版本基线的替换任务，实际发布时还会再次核对版本。
    return await _submit(db, prepared, actor, document_id=document_id,
                         # 设置「替换操作预期的旧版本」参数，使当前调用使用明确的配置或元数据。
                         base_revision=old_revision, request_id=request_id, background=background)


# 注册知识库 GET 路由；具体路径由路由前缀和下方处理函数的参数共同组成。
@router.get("/jobs/{job_id}")
# 定义函数：允许已登录用户读取共享索引任务的真实状态，不暴露内部异常正文。
async def get_index_job(job_id: uuid.UUID, current_user: dict = Depends(get_current_user),
                        # 声明「请求级异步数据库会话」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
                        db: AsyncSession = Depends(get_db)):
    # 将已认证用户标识转换成 UUID，无法转换时返回 401。
    _actor(current_user)
    # 准备「索引任务对象」：等待异步操作完成，再继续当前请求。
    job = await db.get(KnowledgeIndexJob, job_id)
    # 没有找到可复用/领取的任务时进入相应分支；提交接口会创建，worker 则返回空任务。
    if job is None:
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(404, "索引任务不存在")
    # 返回任务真实状态的公开数据，而不是推测后台工作已经完成。
    return {"code":0, "data":_job_data(job), "message":"ok"}


# 注册知识库 POST 路由；具体路径由路由前缀和下方处理函数的参数共同组成。
@router.post("/jobs/{job_id}/retry")
# 定义函数：锁定失败任务并检查管理权限，再重新入队并记录重试审计。
async def retry_index_job(job_id: uuid.UUID, current_user: dict = Depends(get_current_user),
                          # 声明「请求级异步数据库会话」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
                          db: AsyncSession = Depends(get_db)):
    # 准备「通过校验的用户 UUID」：验证用户 UUID 与服务端管理策略；shared 放行，admin 检查管理员名单。
    actor = require_knowledge_manager(current_user)
    # 准备「索引任务对象」：等待异步操作完成，再继续当前请求。
    job = await db.scalar(select(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job_id).with_for_update())
    # 没有找到可复用/领取的任务时进入相应分支；提交接口会创建，worker 则返回空任务。
    if job is None:
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(404, "索引任务不存在")
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 仅将未达重试上限的失败任务重新入队，并清空旧租约信息。
        retry_job(db, job)
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except ValueError as exc:
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(409, str(exc)) from exc
    # 把该项加入去重集合或当前数据库事务。
    db.add(KnowledgeAuditEvent(actor_id=actor, document_id=job.document_id, action="index_retry", details={"job_id":str(job_id)}))
    # 提交当前数据库事务，使任务状态或文档变更正式生效，同时释放该事务持有的行锁。
    await db.commit()
    # 返回重新排队后的任务状态；真正执行由 worker 后续领取。
    return {"code":0, "data":_job_data(job), "message":"queued"}


# 注册知识库 POST 路由；具体路径由路由前缀和下方处理函数的参数共同组成。
@router.post("/{document_id}/reindex")
# 定义函数：从当前版本的完整归档重建索引；缺失原文件时要求重新上传。
async def reindex_document(document_id: uuid.UUID, current_user: dict = Depends(get_current_user),
                           # 声明「请求级异步数据库会话」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
                           db: AsyncSession = Depends(get_db), request_id: uuid.UUID | None = None):
    # 从 src.agent.knowledge_archive 导入 read_archive，供下方相关数据结构与业务步骤。
    from src.agent.knowledge_archive import read_archive
    # 准备「通过校验的用户 UUID」：验证用户 UUID 与服务端管理策略；shared 放行，admin 检查管理员名单。
    actor = require_knowledge_manager(current_user)
    # 准备「文档元数据对象」：等待异步操作完成，再继续当前请求。
    document = await db.get(KnowledgeDocument, document_id)
    # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
    if document is None or document.deleted_at is not None:
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(404, "文档不存在")
    # 准备「(revision, filename, sha)」：把本步骤相关值放在同一有序组合中。
    revision, filename, sha = document.revision, document.filename, document.sha256
    # 回滚并释放当前事务；可能是出错恢复，也可能是主动避免在文件解析期间占用事务。
    await db.rollback()
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 准备「原始文件字节」：等待异步操作完成，再继续当前请求。
        raw = await run_in_threadpool(read_archive, revision, sha)
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except (OSError, ValueError) as exc:
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(409, "当前版本无完整归档，请重新上传文件") from exc
    # 从 pathlib 导入 PurePath，供下方按路径对象处理文件。
    from pathlib import PurePath
    # 准备「解析完成的文档」：封装原文件元数据和解析结果。
    prepared = PreparedDocument(filename, CONTENT_TYPES[PurePath(filename).suffix.lower()],
                                # 用当前归档的原文件大小与已核实摘要构造任务输入，解析仍由后台 worker 完成。
                                len(raw), sha, [], raw)
    # 从归档重建始终以后台任务提交，并携带读取时的旧版本基线。
    return await _submit(db, prepared, actor, document_id=document_id, base_revision=revision,
                         # 设置「可选幂等请求 UUID」参数，使当前调用使用明确的配置或元数据。
                         request_id=request_id, background=True)


# 注册知识库 GET 路由；具体路径由路由前缀和下方处理函数的参数共同组成。
@router.get("/{document_id}/audit")
# 定义函数：有界查询指定文档的管理审计记录，按最近发生时间排序。
async def document_audit(document_id: uuid.UUID, current_user: dict = Depends(get_current_user),
                          # 声明「请求级异步数据库会话」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
                          db: AsyncSession = Depends(get_db), limit: int = Query(50, ge=1, le=100)):
    # 将已认证用户标识转换成 UUID，无法转换时返回 401。
    _actor(current_user)
    # 准备「数据库行或文档表格行」：取出本次有界查询的全部返回行。
    rows = (await db.scalars(select(KnowledgeAuditEvent).where(KnowledgeAuditEvent.document_id == document_id)
        # 继续组合本次操作的参数、条件或结果字段。
        .order_by(KnowledgeAuditEvent.created_at.desc()).limit(limit))).all()
    # 将审计行转成 JSON 友好数据，只返回请求数量以内的最新记录。
    return {"code":0, "data":[{"actor_id":str(r.actor_id), "action":r.action,
        # 写入「结构化审计信息」字段，与同一条记录的其他字段共同组成结果。
        "details":r.details, "created_at":r.created_at.isoformat()} for r in rows], "message":"ok"}


# 注册知识库 DELETE 路由；具体路径由路由前缀和下方处理函数的参数共同组成。
@router.delete("/{document_id}")
# 定义函数：锁住并软删除文档后提交，再尝试清理向量；物理清理失败不会恢复检索可见性。
async def delete_document(
    # 声明「逻辑文档 ID」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    document_id: uuid.UUID,
    # 声明「JWT 认证依赖返回的当前用户」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    current_user: dict = Depends(get_current_user),
    # 声明「请求级异步数据库会话」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    db: AsyncSession = Depends(get_db),
# 结束函数参数与返回类型声明，下面进入处理正文。
):
    # 准备「通过校验的用户 UUID」：验证用户 UUID 与服务端管理策略；shared 放行，admin 检查管理员名单。
    actor = require_knowledge_manager(current_user)
    # 准备「文档元数据对象」：等待异步操作完成，再继续当前请求。
    document = await db.scalar(
        # 定位要删除的文档，下一行会加行锁以与发布事务互斥。
        select(KnowledgeDocument).where(KnowledgeDocument.id == document_id)
        # 继续组合本次操作的参数、条件或结果字段。
        .with_for_update().execution_options(populate_existing=True)
    # 结束当前SQL 条件/对象构造，后续语句继续处理这一结果。
    )
    # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
    if document is None or document.deleted_at is not None:
        # 结束当前请求并返回明确的 HTTP 错误，不把拒绝、冲突或故障伪报为成功。
        raise HTTPException(404, "文档不存在")
    # 先用软删除使 SQL 权威目录不可见，后续向量清理可以安全重试。
    document.deleted_at = datetime.now(UTC)
    # 准备「最近更新时间」：读取 deleted_at。
    document.updated_at = document.deleted_at
    # 把该项加入去重集合或当前数据库事务。
    db.add(KnowledgeAuditEvent(actor_id=actor, document_id=document_id, action="deleted", details={}))
    # 提交当前数据库事务，使任务状态或文档变更正式生效，同时释放该事务持有的行锁。
    await db.commit()
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 把同步归档/索引操作放到线程池等待，避免阻塞异步请求的事件循环。
        await run_in_threadpool(delete_document_chunks, document_id)
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except Exception:
        # The document is immediately hidden by the committed soft delete.
        # 记录当前阶段的状态/故障或统计，便于排查；这里不输出完整上传正文。
        logger.exception("Could not purge deleted knowledge document=%s", document_id)
    # 返回软删除成功；残留向量可以由后续对账重试清理。
    return {"code": 0, "data": None, "message": "ok"}
