# 模块职责：持久化索引任务，通过短数据库事务、租约和发布前校验恢复故障并阻止过期 worker 写入有效版本。
"""Durable indexing with lease fencing and immutable, archived revisions."""
# 导入 hashlib，用于计算文件、配置与模型内容摘要。
import hashlib
# 导入 logging，用于记录模块运行状态与可排查的故障信息。
import logging
# 导入 os，用于读取环境变量并执行必要的文件系统操作。
import os
# 导入 threading，用于启动心跳线程，并通过 Event 在任务线程和心跳线程间传递停止/失效信号。
import threading
# 导入 uuid，用于验证 UUID 或生成稳定/随机标识。
import uuid
# 从 datetime 导入 datetime, timedelta, timezone，供下方统一使用带时区的时间。
from datetime import datetime, timedelta, timezone
# 从 pathlib 导入 Path，供下方按路径对象处理文件。
from pathlib import Path
# 从 sqlalchemy 导入 and_, or_, select, update，供下方构造数据库字段与参数化查询。
from sqlalchemy import and_, or_, select, update
# 从 sqlalchemy.orm 导入 Session，供下方声明 ORM 字段或管理短会话。
from sqlalchemy.orm import Session
# 从 api.knowledge_models 导入 KnowledgeAuditEvent, KnowledgeIndexJob，供下方相关数据结构与业务步骤。
from api.knowledge_models import KnowledgeAuditEvent, KnowledgeIndexJob
# 从 api.models 导入 KnowledgeDocument，供下方相关数据结构与业务步骤。
from api.models import KnowledgeDocument
# 从 src.agent.knowledge_archive 导入 archive_bytes, cached_prepared，供下方相关数据结构与业务步骤。
from src.agent.knowledge_archive import archive_bytes, cached_prepared
# 从 src.agent.knowledge_catalog 导入 replace_chunk_catalog，供下方相关数据结构与业务步骤。
from src.agent.knowledge_catalog import replace_chunk_catalog
# 从 src.agent.knowledge_ingest 导入 prepare_document，供下方相关数据结构与业务步骤。
from src.agent.knowledge_ingest import prepare_document

# 准备「每次任务租约时长（秒）」：设置为 180。
LEASE_SECONDS = 180
# 准备「允许的累计执行次数上限」：设置为 3。
MAX_ATTEMPTS = 3
# 准备「当前模块日志记录器」：取得当前模块的日志对象。
logger = logging.getLogger(__name__)


# 定义函数：快照解析源码、分块/OCR 开关和本地模型指纹，防止重试偷偷换用另一套索引配置。
def index_config():
    # 从 src.agent.knowledge_versions 导入 embedding_version，供下方相关数据结构与业务步骤。
    from src.agent.knowledge_versions import embedding_version
    # 读取解析器完整源码以计算版本哈希；注释也属于源码，因此增加注释会使旧任务的配置指纹变化。
    source = Path(__file__).with_name('knowledge_ingest.py').read_bytes()
    # 准备「本地 Embedding 模型目录」：转换成字符串。
    model_path = str(Path('~/models/bge-base-zh-v1.5').expanduser())
    # 返回本次解析与模型配置快照，worker 执行前会与任务保存的快照严格比较。
    return {'parser_sha256': hashlib.sha256(source).hexdigest(),
            # 写入「chunking_version」字段，与同一条记录的其他字段共同组成结果。
            'chunking_version': os.getenv('KNOWLEDGE_CHUNKING_VERSION', '2'),
            # 写入「ocr」字段，与同一条记录的其他字段共同组成结果。
            'ocr': os.getenv('KNOWLEDGE_OCR', '0'),
            # 写入「ocr_lang」字段，与同一条记录的其他字段共同组成结果。
            'ocr_lang': os.getenv('KNOWLEDGE_OCR_LANG', 'chi_sim+eng'),
            # 写入「pdf_layout」字段，与同一条记录的其他字段共同组成结果。
            'pdf_layout': os.getenv('KNOWLEDGE_PDF_LAYOUT', '0'),
            # 写入「embedding」字段，与同一条记录的其他字段共同组成结果。
            'embedding': model_path, 'embedding_version':embedding_version(model_path)}


# 定义函数：用请求 ID 生成稳定的文档/版本标识，先归档原文件，再创建待入队任务对象。
def make_job(*, prepared, actor_id, document_id=None, base_revision=None, request_id=None):
    # 有请求 ID 时复用它实现幂等；未提供时明确创建新任务。
    job_id = request_id or uuid.uuid4()
    # 从任务 ID 确定性派生 revision；同一请求重试继续指向同一修订。
    revision = uuid.uuid5(job_id, 'revision')
    # 在任务入库前保存不可变原文件，后续 worker 可脱离 HTTP 请求重建索引。
    archive_bytes(revision, prepared.raw)
    # 创建排队任务对象；这里只归档并构造对象，数据库提交由调用者完成。
    return KnowledgeIndexJob(id=job_id, document_id=document_id or uuid.uuid5(job_id, 'document'),
        # 设置「不可变文档版本」参数，使当前调用使用明确的配置或元数据。
        revision=revision, base_revision=base_revision, actor_id=actor_id,
        # 设置「原文件名称」参数，使当前调用使用明确的配置或元数据。
        filename=prepared.filename, sha256=prepared.sha256, status='queued',
        # 设置「任务已执行次数」参数，使当前调用使用明确的配置或元数据。
        attempts=0, index_config=index_config())


# 定义函数：向当前事务加入审计对象；与业务修改一起提交，避免成功状态与审计记录分离。
def audit(session, actor_id, document_id, action, **details):
    # 把该项加入去重集合或当前数据库事务。
    session.add(KnowledgeAuditEvent(actor_id=actor_id, document_id=document_id, action=action, details=details))


# 定义函数：用短事务领取一个任务，跳过其他进程锁住的行，并写入新的租约令牌和到期时间。
def claim(engine, job_id=None):
    # 准备「当前 UTC 时间」：读取指定时区的当前时间。
    now = datetime.now(timezone.utc)
    # 开启短数据库事务；正常退出自动提交，异常则回滚并释放事务内的行锁。
    with Session(engine, expire_on_commit=False) as session, session.begin():
        # 执行当前参数化 SQL 语句；事务边界仍由外层上下文管理。
        session.execute(update(KnowledgeIndexJob).where(KnowledgeIndexJob.status == 'running',
            # 只把已过期且次数耗尽的 running 任务收尾为失败；尚有重试机会的任务留给后续领取。
            KnowledgeIndexJob.lease_until < now, KnowledgeIndexJob.attempts >= MAX_ATTEMPTS).values(
            # 设置「任务/处理状态」参数，使当前调用使用明确的配置或元数据。
            status='failed', error='租约过期且已达到重试上限', lease_until=None))
        # 先筛出未耗尽执行次数的任务，再由后续状态和到期条件决定哪些可以领取。
        statement = select(KnowledgeIndexJob).where(KnowledgeIndexJob.attempts < MAX_ATTEMPTS,
            # 待领取集合包括 queued，以及下行限定的租约已过期 running 任务。
            or_(KnowledgeIndexJob.status == 'queued', and_(KnowledgeIndexJob.status == 'running',
                 # 接管只能发生在旧租约到期后；候选按创建时间排序，优先处理更早任务。
                 KnowledgeIndexJob.lease_until < now))).order_by(KnowledgeIndexJob.created_at)
        # 只在指定任务 ID 时缩小领取范围，否则从队列选择可运行任务。
        if job_id is not None:
            # 准备「尚未执行的 SQL 查询」：为 SQL 语句添加过滤条件。
            statement = statement.where(KnowledgeIndexJob.id == job_id)
        # 锁住一个可领取任务并跳过已被其他事务锁住的行；多个 worker 不会同时领取同一行。
        job = session.scalar(statement.with_for_update(skip_locked=True).limit(1))
        # 没有找到可复用/领取的任务时进入相应分支；提交接口会创建，worker 则返回空任务。
        if job is None:
            # 没有可领取/交付的任务时返回 None，调用者可继续轮询或退出本轮。
            return None
        # 在领取事务内把任务从排队/过期状态改为执行中。
        job.status = 'running'
        # 为本次领取生成全新令牌；接管同一任务时也会变化，用它识别过期执行者。
        job.lease_owner = str(uuid.uuid4())
        # 将当前租约设置为 180 秒；之后由心跳按同一令牌延长。
        job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        # 记录本次领取任务的 UTC 时间，供状态排查。
        job.updated_at = now
        # 记录本次真实执行，防止异常任务无限重试。
        job.attempts += 1
        # 开始新一次执行时清空上次错误；旧错误类型仍可从失败审计中查看。
        job.error = None
        # 将待写 ORM 对象发送到数据库但不提交；最终提交或回滚仍由外层事务统一控制。
        session.flush()
        # 将任务从会话解绑；事务提交后继续使用已加载数据，不把数据库事务跨越整个向量化过程。
        session.expunge(job)
        # 返回已经写入 running 状态及新租约的任务快照；离开 with 时领取事务提交并释放行锁。
        return job


# 定义函数：后台心跳每 30 秒按任务 ID 和租约令牌续期；续期失败时通知主线程失去处理权。
def _heartbeat(engine, job, stop, lost):
    # 每隔 30 秒尝试一次续租；等待可以被 stop 信号立即唤醒，避免退出时多等一个周期。
    while not stop.wait(30):
        # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
        try:
            # 开启短数据库事务；正常退出自动提交，异常则回滚并释放事务内的行锁。
            with Session(engine) as session, session.begin():
                # 执行带任务 ID、状态及租约令牌约束的续租更新，并保留影响行数检查所有权。
                changed = session.execute(update(KnowledgeIndexJob).where(
                    # 续租更新必须命中同一个仍在执行的任务。
                    KnowledgeIndexJob.id == job.id, KnowledgeIndexJob.status == 'running',
                    # 续租还必须匹配本次领取的令牌；旧 worker 不能延长新 worker 的租约。
                    KnowledgeIndexJob.lease_owner == job.lease_owner).values(
                    # 设置「租约到期时间」参数，使当前调用使用明确的配置或元数据。
                    lease_until=datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)))
                # 续租必须恰好更新自己拥有的一行；更新不到说明任务状态或所有权已经改变。
                if changed.rowcount != 1:
                    # 标记本地已无法确认租约；主任务即使完成向量写入，也必须拒绝过期发布。
                    lost.set()
                    # 当前步骤已完成或无需继续，直接结束本函数。
                    return
        # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
        except Exception:
            # 标记本地已无法确认租约；主任务即使完成向量写入，也必须拒绝过期发布。
            lost.set()
            # 当前步骤已完成或无需继续，直接结束本函数。
            return


# 定义函数：在调用方已锁住任务的事务里锁定文档，核对旧版本，再原子发布元数据和分块目录。
def publish(session, job, prepared):
    # 函数约定：在调用方已锁住任务的事务里锁定文档，核对旧版本，再原子发布元数据和分块目录。
    """Called under the job lease lock and document lock, in one SQL transaction."""
    # 准备「文档元数据对象」：执行查询并取得一个对象/标量。
    document = session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == job.document_id)
                              # 继续组合持锁文档查询。
                              .with_for_update())
    # 没有旧版本基线表示新建文档；有基线的任务必须走替换校验。
    if job.base_revision is None:
        # 新建任务发现同 ID 文档已存在时拒绝覆盖；这不是一个可随意执行的更新。
        if document is not None:
            # 拒绝这次发布，通知调用者发生了文档不存在、删除或版本冲突。
            return False
        # 准备「文档元数据对象」：构造文档权威目录记录。
        document = KnowledgeDocument(id=job.document_id, revision=job.revision,
            # 设置「原文件名称」参数，使当前调用使用明确的配置或元数据。
            filename=prepared.filename, content_type=prepared.content_type,
            # 设置「原文件字节数」参数，使当前调用使用明确的配置或元数据。
            size_bytes=prepared.size_bytes, sha256=prepared.sha256,
            # 设置「分块数量」参数，使当前调用使用明确的配置或元数据。
            chunk_count=len(prepared.chunks), uploaded_by=job.actor_id)
        # 把该项加入去重集合或当前数据库事务。
        session.add(document)
    # 进入前一条件未满足时的分支，采用对应的回退处理。
    else:
        # 文档不存在、已删除或已被他人替换时拒绝发布；旧任务不能复活文档或覆盖新版本。
        if document is None or document.deleted_at is not None or document.revision != job.base_revision:
            # 拒绝这次发布，通知调用者发生了文档不存在、删除或版本冲突。
            return False
        # 准备「不可变文档版本」：读取 不可变文档版本。
        document.revision = job.revision
        # 准备「原文件名称」：读取 原文件名称。
        document.filename = prepared.filename
        # 准备「文件 MIME 类型」：读取 文件 MIME 类型。
        document.content_type = prepared.content_type
        # 准备「原文件字节数」：读取 原文件字节数。
        document.size_bytes = prepared.size_bytes
        # 准备「原文件 SHA-256 摘要」：读取 原文件 SHA-256 摘要。
        document.sha256 = prepared.sha256
        # 准备「分块数量」：读取数量或长度。
        document.chunk_count = len(prepared.chunks)
        # 准备「上传者 UUID」：读取 执行操作的用户 UUID。
        document.uploaded_by = job.actor_id
        # 准备「最近更新时间」：读取指定时区的当前时间。
        document.updated_at = datetime.now(timezone.utc)
    # 将待写 ORM 对象发送到数据库但不提交；最终提交或回滚仍由外层事务统一控制。
    session.flush()
    # 在同一事务写入该有效 revision 的分块/关键词目录，与文档版本切换一起提交。
    replace_chunk_catalog(session, job.document_id, job.revision, prepared.chunks)
    # 向当前事务加入审计对象；与业务修改一起提交，避免成功状态与审计记录分离。
    audit(session, job.actor_id, job.document_id, 'indexed', job_id=str(job.id), revision=str(job.revision))
    # 报告本步骤已接受并完成，事务是否提交仍由外层上下文决定。
    return True


# 定义函数：领取任务并在事务外解析/写向量；最后重新加锁校验租约，决定发布、冲突或失败。
def run_job(engine, job_id=None, *, indexer=None, cleaner=None, heartbeat=True):
    # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
    if indexer is None or cleaner is None:
        # 从 src.agent.knowledge_store 导入 index_document, delete_document_chunks，供下方相关数据结构与业务步骤。
        from src.agent.knowledge_store import index_document, delete_document_chunks
        # 准备「(indexer, cleaner)」：把本步骤相关值放在同一有序组合中。
        indexer, cleaner = indexer or index_document, cleaner or delete_document_chunks
    # 准备「索引任务对象」：用短事务领取一个任务，跳过其他进程锁住的行，并写入新的租约令牌和到期时间。
    job = claim(engine, job_id)
    # 没有找到可复用/领取的任务时进入相应分支；提交接口会创建，worker 则返回空任务。
    if job is None:
        # 没有可领取/交付的任务时返回 None，调用者可继续轮询或退出本轮。
        return None
    # 创建两个线程间通知信号：stop 请求结束心跳，lost 表示租约不可再信任；它们不是任务互斥锁。
    stop, lost = threading.Event(), threading.Event()
    # 创建守护心跳线程；真正的任务互斥依靠数据库状态与租约，而不是该线程本身。
    thread = threading.Thread(target=_heartbeat, args=(engine, job, stop, lost), daemon=True)
    # 允许测试关闭心跳；正常 worker 会启动并在任务结束时停止心跳线程。
    if heartbeat:
        # 启动心跳，让耗时解析和向量写入期间也能续租。
        thread.start()
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 比较任务提交时与当前 worker 的解析/模型版本；不允许重试悄悄换用另一套处理配置。
        if job.index_config != index_config():
            # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
            raise ValueError('索引配置已改变，请恢复原 worker 配置或重新提交新 revision')
        # 读取或首次固定解析清单；重试复用同一分块序列，保持片段 ID 与数量稳定。
        prepared = cached_prepared(job, prepare_document)
        # 在没有长时间持有 SQL 事务的阶段写入向量；向量准备好之后仍需通过发布前校验。
        indexer(job.document_id, job.revision, prepared.filename, prepared.chunks)
        # 开启短数据库事务；正常退出自动提交，异常则回滚并释放事务内的行锁。
        with Session(engine) as session, session.begin():
            # 重新锁定任务权威行；不能仅凭领取时保留的对象判断自己仍拥有发布权。
            current = session.scalar(select(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job.id).with_for_update())
            # 准备「本次租约的到期时间」：读取 租约到期时间。
            expires = current.lease_until
            # 对租约时间的时区/有效性作进一步检查。
            if expires is not None and expires.tzinfo is None:
                # 为不带时区的离线数据库时间补上 UTC，保证后续与 UTC 当前时间可比较。
                expires = expires.replace(tzinfo=timezone.utc)
            # 发布前同时核对本地续租状态、数据库令牌与任务状态，防止旧 worker 提交结果。
            if (lost.is_set() or current.lease_owner != job.lease_owner or current.status != 'running'
                    # 继续要求租约存在且尚未到期；即使令牌未被接管，也不能使用已过期的处理权。
                    or expires is None or expires <= datetime.now(timezone.utc)):
                # 停止发布，保留接管者的处理结果；此处也不删除同一 revision 的向量，以免误删新 worker 的写入。
                return 'lease_lost'
            # 在已锁住任务、确认租约有效后尝试原子发布；文档版本冲突会返回 False。
            accepted = publish(session, job, prepared)
            # 将发布结果与任务状态放在同一 SQL 事务中：成功才标记 succeeded，版本竞争失败则标记 conflict。
            current.status = 'succeeded' if accepted else 'conflict'
            # 任务已进入终态，清空租约到期时间，不再需要续期。
            current.lease_until = None
            # 准备「最近更新时间」：读取指定时区的当前时间。
            current.updated_at = datetime.now(timezone.utc)
        # Publication is already committed. Cleanup failure cannot roll it back.
        # 仅在新版本已发布且确实存在旧修订时，尝试异步于发布事务的旧向量清理。
        if accepted and job.base_revision:
            # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
            try:
                # 只清理刚被替换的旧修订；新修订已经提交，因此清理失败也不能回滚发布。
                cleaner(job.document_id, job.base_revision)
            # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
            except Exception:
                # 记录当前阶段的状态/故障或统计，便于排查；这里不输出完整上传正文。
                logger.warning('Inactive knowledge revision cleanup deferred: job=%s revision=%s',
                               # 日志只携带任务 ID 和旧版本标识，便于定位清理失败，不打印完整文档正文。
                               job.id, job.base_revision)
        # 返回当前处理结果，由调用方继续决定下一步。
        return 'succeeded' if accepted else 'conflict'
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except Exception as exc:
        # 记录当前阶段的状态/故障或统计，便于排查；这里不输出完整上传正文。
        logger.error('Knowledge indexing failed: job=%s type=%s', job.id, type(exc).__name__)
        # Check ownership before recording failure; never erase another worker's writes.
        # 开启短数据库事务；正常退出自动提交，异常则回滚并释放事务内的行锁。
        with Session(engine) as session, session.begin():
            # 重新锁定任务权威行；不能仅凭领取时保留的对象判断自己仍拥有发布权。
            current = session.scalar(select(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job.id).with_for_update())
            # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
            if current.lease_owner == job.lease_owner and current.status == 'running':
                # 仅在仍拥有该任务时记录失败，避免旧 worker 把接管者的结果改回失败。
                current.status = 'failed'
                # 保存异常类型与有长度上限的诊断文本；接口另行输出通用错误，避免直接暴露内部详情。
                current.error = type(exc).__name__ + ': ' + str(exc)[:400]
                # 任务已进入终态，清空租约到期时间，不再需要续期。
                current.lease_until = None
                # 准备「最近更新时间」：读取指定时区的当前时间。
                current.updated_at = datetime.now(timezone.utc)
                # 向当前事务加入审计对象；与业务修改一起提交，避免成功状态与审计记录分离。
                audit(session, job.actor_id, job.document_id, 'index_failed', job_id=str(job.id),
                      # 设置「attempt」参数，使当前调用使用明确的配置或元数据。
                      attempt=job.attempts, error_type=type(exc).__name__)
        # 返回当前处理结果，由调用方继续决定下一步。
        return 'failed'
    # 无论成功、失败或提前返回，都执行下方资源清理/统计逻辑。
    finally:
        # 任务结束后通知心跳线程停止续租。
        stop.set()
        # 允许测试关闭心跳；正常 worker 会启动并在任务结束时停止心跳线程。
        if heartbeat:
            # 最多等待心跳线程退出 2 秒，不因线程收尾无限阻塞主任务。
            thread.join(timeout=2)


# 定义函数：仅将未达重试上限的失败任务重新入队，并清空旧租约信息。
def retry_job(session, job):
    # 根据持久任务状态选择操作，不能把排队、执行中、冲突和成功混为一谈。
    if job.status != 'failed' or job.attempts >= MAX_ATTEMPTS:
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError('仅可重试未达到次数上限的失败任务')
    # 准备「任务/处理状态」：进入待领取队列。
    job.status = 'queued'
    # 开始新一次执行时清空上次错误；旧错误类型仍可从失败审计中查看。
    job.error = None
    # 重新入队时清空旧执行者令牌，下一次领取会生成全新的租约令牌。
    job.lease_owner = None
    # 重新入队的任务尚未授予处理权，因此不保留旧租约到期时间。
    job.lease_until = None


# 定义函数：盘点修订，锁住权威记录后确认其已失效；默认只预览，显式 apply 才删除向量和目录。
def reconcile(engine, *, cleaner=None, apply=False, scan_vectors=False):
    # 函数约定：盘点修订，锁住权威记录后确认其已失效；默认只预览，显式 apply 才删除向量和目录。
    """Catalog rows and finished jobs are a bounded inventory of known revisions."""
    # 从 api.knowledge_models 导入 KnowledgeChunkRecord，供下方相关数据结构与业务步骤。
    from api.knowledge_models import KnowledgeChunkRecord
    # 从 src.agent.knowledge_store 导入 delete_document_chunks，供下方相关数据结构与业务步骤。
    from src.agent.knowledge_store import delete_document_chunks
    # 准备「实际删除向量的函数」：组合多个必要条件进行判断。
    cleaner = cleaner or delete_document_chunks
    # 借用同步数据库会话，退出上下文时释放连接，不长期占用事务。
    with Session(engine) as session:
        # 准备「已知文档/修订对集合」：建立去重集合。
        known = set(session.execute(select(KnowledgeChunkRecord.document_id, KnowledgeChunkRecord.revision).distinct()).all())
        # 增量更新当前统计或状态映射。
        known.update(session.execute(select(KnowledgeIndexJob.document_id, KnowledgeIndexJob.revision).where(
            # 把终态任务涉及的修订加入盘点；是否可删除仍要在持锁事务里重新判断。
            KnowledgeIndexJob.status.in_(['failed', 'conflict', 'succeeded', 'cancelled']))).all())
    # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
    if scan_vectors:
        # 从 src.agent.knowledge_store 导入 get_client, KNOWLEDGE_COLLECTION，供下方相关数据结构与业务步骤。
        from src.agent.knowledge_store import get_client, KNOWLEDGE_COLLECTION
        # 准备「client」：延迟取得共享 Milvus 客户端，避免导入模块时立刻连接外部服务。
        client = get_client()
        # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
        if client.has_collection(KNOWLEDGE_COLLECTION):
            # 准备「分批盘点 Milvus 元数据的游标」：调用 client.query_iterator 完成当前步骤。
            iterator = client.query_iterator(collection_name=KNOWLEDGE_COLLECTION, batch_size=1000,
                # 设置「需要返回的元数据字段」参数，使当前调用使用明确的配置或元数据。
                output_fields=['document_id','revision'], consistency_level='Strong')
            # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
            try:
                # 持续处理下一轮任务、候选窗口或盘点批次，直到内部退出条件成立。
                while True:
                    # 准备「当前一批分块或元数据」：调用 iterator.next 完成当前步骤。
                    batch = iterator.next()
                    # 确认本步骤所需的输入、结果或资源是否缺失，缺失时走明确回退路径。
                    if not batch:
                        # 结束当前循环：已经达到结果上限，或已经没有下一批可读取数据。
                        break
                    # 逐项处理当前 已知修订或一批向量元数据。
                    for row in batch:
                        # 把该项加入去重集合或当前数据库事务。
                        known.add((uuid.UUID(row['document_id']), uuid.UUID(row['revision'])))
            # 无论成功、失败或提前返回，都执行下方资源清理/统计逻辑。
            finally:
                # 无论执行结果如何都释放当前资源。
                iterator.close()
    # 准备「(candidates, cleaned)」：把本步骤相关值放在同一有序组合中。
    candidates, cleaned = [], []
    # 逐项处理当前 已知修订或一批向量元数据。
    for doc_id, revision in known:
        # 开启短数据库事务；正常退出自动提交，异常则回滚并释放事务内的行锁。
        with Session(engine) as session, session.begin():
            # 清理前先锁住相关任务，避免重试/领取与物理删除同一 revision 并发发生。
            task = session.scalar(select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == doc_id,
                # 锁住当前 revision 的任务行，与重试和领取使用同一套数据库互斥规则。
                KnowledgeIndexJob.revision == revision).with_for_update())
            # 排队或执行中的 revision 还可能合法发布，清理程序必须跳过。
            if task is not None and task.status in {'queued', 'running'}:
                # 保护当前有效版本或正在处理的版本，跳过本次删除候选。
                continue
            # Hold the authority row lock through deletion so a concurrent publication cannot race cleanup.
            # 准备「当前文档对象/文档标识」：执行查询并取得一个对象/标量。
            doc = session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id).with_for_update())
            # 当前仍有效的 revision 永远不进入清理候选，防止删除正在提供检索的向量。
            if doc is not None and doc.deleted_at is None and doc.revision == revision:
                # 保护当前有效版本或正在处理的版本，跳过本次删除候选。
                continue
            # 准备「是否有该修订的排队或执行中任务」：执行查询并取得一个对象/标量。
            busy = session.scalar(select(KnowledgeIndexJob.id).where(KnowledgeIndexJob.document_id == doc_id,
                # 再次确认同修订没有等待或执行中的任务，避免在它尚可能发布时清理向量。
                KnowledgeIndexJob.revision == revision, KnowledgeIndexJob.status.in_(['queued','running'])))
            # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
            if busy:
                # 保护当前有效版本或正在处理的版本，跳过本次删除候选。
                continue
            # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
            candidates.append((str(doc_id), str(revision)))
            # 只有显式要求 apply 才真正删除；默认盘点不改向量数据。
            if apply:
                # 仅删除已核实失效的文档/修订对，调用期间仍持有权威文档锁。
                cleaner(doc_id, revision)
                # 从 sqlalchemy 导入 delete，供下方构造数据库字段与参数化查询。
                from sqlalchemy import delete
                # 执行当前参数化 SQL 语句；事务边界仍由外层上下文管理。
                session.execute(delete(KnowledgeChunkRecord).where(KnowledgeChunkRecord.document_id == doc_id,
                                                                    # 把目录删除范围进一步限制到这一 revision，保留同文档的其他版本。
                                                                    KnowledgeChunkRecord.revision == revision))
                # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
                cleaned.append((str(doc_id), str(revision)))
    # 返回已盘点与已删除的修订列表，并明确是否执行了 apply。
    return {'candidates': candidates, 'cleaned': cleaned, 'apply': apply}
