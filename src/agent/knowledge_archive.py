# 模块职责：按版本 UUID 私有归档原文件和首次解析结果；哈希校验防止读取错误内容，原子创建避免并发覆盖。
"""Private immutable raw uploads addressed by revision UUID; never by filename."""
# 导入 hashlib，用于计算文件、配置与模型内容摘要。
import hashlib
# 导入 os，用于读取环境变量并执行必要的文件系统操作。
import os
# 导入 tempfile，用于创建私有临时文件或临时目录。
import tempfile
# 导入 uuid，用于验证 UUID 或生成稳定/随机标识。
import uuid
# 从 pathlib 导入 Path，供下方按路径对象处理文件。
from pathlib import Path

# 准备「原文件归档字节上限」：按当前配置合并、累计或计算该值。
MAX_BYTES = 10 * 1024 * 1024


# 定义函数：取得配置或默认的私有归档目录，创建后返回规范化绝对路径。
def archive_root():
    # 准备「未配置时使用的默认值」：按当前配置合并、累计或计算该值。
    default = Path(__file__).resolve().parents[2] / 'output/knowledge'
    # 准备「资源根路径」：调用 .resolve 完成当前步骤。
    root = Path(os.getenv('KNOWLEDGE_ARCHIVE_DIR', str(default))).expanduser().resolve()
    # 创建仅当前用户可访问的归档目录；parents 允许创建父目录，已存在时不重复报错。
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # 返回已规范化的归档目录，由调用者拼接 UUID 文件名。
    return root


# 定义函数：把原文件绑定到不可变 revision：同一版本只接受同一份内容。
def archive_bytes(revision, raw):
    # 准备「不可变文档版本」：转换成字符串。
    revision = str(uuid.UUID(str(revision)))
    # 确认本步骤所需的输入、结果或资源是否缺失，缺失时走明确回退路径。
    if not raw or len(raw) > MAX_BYTES:
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError('归档文件为空或超过上限')
    # 准备「原始字节的 SHA-256」：将内容摘要转换成十六进制字符串。
    sha = hashlib.sha256(raw).hexdigest()
    # 准备「最终归档文件路径」：按当前配置合并、累计或计算该值。
    target = archive_root() / (revision + '.raw')
    # 在最终文件同目录建立私有临时文件，完整写入后再原子发布。
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        # 准备「待原子发布的临时文件路径」：构造文件系统路径对象。
        temp = Path(handle.name)
        # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
        try:
            # 把原文件字节写入私有临时文件，完整刷新后才发布最终归档路径。
            handle.write(raw)
            # 先将 Python 文件缓冲写入系统，再执行后续 fsync。
            handle.flush()
            # 请求将临时文件内容同步到磁盘，避免只保留在 Python/系统写入缓冲区。
            os.fsync(handle.fileno())
        # 连取消和中断也执行临时文件收尾，随后继续向外传播原始异常。
        except BaseException:
            # 清理这次创建的临时文件；已被清理时不额外报错。
            temp.unlink(missing_ok=True)
            # 继续传播原始异常，保留异常类型和调用堆栈，不将其吞掉。
            raise
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
        try:
            # 通过同目录硬链接原子创建最终归档；已有目标不会被覆盖，避免并发修改同一 revision。
            os.link(temp, target)  # Atomic create; an existing revision is immutable.
        # 处理目标已存在的并发情况；保留规范归档，再检查内容或复用已有解析结果。
        except FileExistsError:
            # 已有原文件必须与本次原始字节摘要一致；同一 revision 不允许绑定不同内容。
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
                raise ValueError('同一 revision 不可归档不同文件')
    # 无论成功、失败或提前返回，都执行下方资源清理/统计逻辑。
    finally:
        # 清理这次创建的临时文件；已被清理时不额外报错。
        temp.unlink(missing_ok=True)
    # 返回原文件内容摘要，后续数据库元数据与归档读取使用同一 SHA-256。
    return sha


# 定义函数：按版本读取原文件，先检查文件类型和体积，再比对预期 SHA-256。
def read_archive(revision, expected_sha):
    # 准备「最终归档文件路径」：按当前配置合并、累计或计算该值。
    target = archive_root() / (str(uuid.UUID(str(revision))) + '.raw')
    # 拒绝符号链接和超限文件，避免归档读取跳转到非预期目标或占用过多内存。
    if target.is_symlink() or target.stat().st_size > MAX_BYTES:
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError('归档文件无效')
    # 准备「原始文件字节」：读取本地文件原始字节。
    raw = target.read_bytes()
    # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError('归档文件哈希不匹配')
    # 仅在原文件通过完整性检查后，返回其原始字节。
    return raw


# 定义函数：固定首次解析结果；重试读取同一分块清单，避免 OCR 波动改变片段数量。
def cached_prepared(job, prepare):
    # 函数约定：固定首次解析结果；重试读取同一分块清单，避免 OCR 波动改变片段数量。
    """Pin the first parsed result so even nondeterministic OCR retries share chunk IDs."""
    # 导入 json，用于序列化结构化数据或构造安全的字符串字面量。
    import json
    # 从 src.agent.knowledge_ingest 导入 PreparedDocument, KnowledgeChunk, MAX_CHUNKS，供下方相关数据结构与业务步骤。
    from src.agent.knowledge_ingest import PreparedDocument, KnowledgeChunk, MAX_CHUNKS
    # 准备「不可变文档版本」：转换成字符串。
    revision = str(uuid.UUID(str(job.revision)))
    # 准备「最终归档文件路径」：按当前配置合并、累计或计算该值。
    target = archive_root() / (revision + '.parsed.json')
    # 准备「原始文件字节」：按版本读取原文件，先检查文件类型和体积，再比对预期 SHA-256。
    raw = read_archive(job.revision, job.sha256)
    # 准备「任务索引配置的稳定摘要」：将内容摘要转换成十六进制字符串。
    configuration = hashlib.sha256(json.dumps(job.index_config, sort_keys=True).encode()).hexdigest()
    # 确认本步骤所需的输入、结果或资源是否缺失，缺失时走明确回退路径。
    if not target.exists():
        # 准备「解析完成的文档」：调用 prepare 完成当前步骤。
        prepared = prepare(job.filename, raw)
        # 准备「待返回或归档的结构化数据」：组织该步骤需要的结构化字段。
        payload = {'sha256':job.sha256, 'configuration':configuration,
            # 写入「原文件名称」字段，与同一条记录的其他字段共同组成结果。
            'filename':prepared.filename,'content_type':prepared.content_type,'size_bytes':prepared.size_bytes,
            # 写入「最终可索引分块列表」字段，与同一条记录的其他字段共同组成结果。
            'chunks':[{'text':c.text,'page':c.page,'section_path':c.section_path} for c in prepared.chunks]}
        # 准备「按稳定键顺序编码的 UTF-8 数据」：将字符串编码成字节以便哈希或字节数限制。
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        # 准备「带摘要的解析清单」：将字符串编码成字节以便哈希或字节数限制。
        record = json.dumps({'digest':hashlib.sha256(encoded).hexdigest(),'payload':payload},
                            # 设置「ensure_ascii」参数，使当前调用使用明确的配置或元数据。
                            ensure_ascii=False, sort_keys=True).encode()
        # 在最终文件同目录建立私有临时文件，完整写入后再原子发布。
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            # 准备「解析清单的临时文件路径」：构造文件系统路径对象。
            temporary=Path(handle.name)
            # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
            try:
                # 写入完整解析清单、刷新缓冲并同步文件内容；之后才发布最终文件名。
                handle.write(record);handle.flush();os.fsync(handle.fileno())
            # 连取消和中断也执行临时文件收尾，随后继续向外传播原始异常。
            except BaseException:
                # 清理解析清单临时文件，不删除已经发布的规范清单。
                temporary.unlink(missing_ok=True)
                # 继续传播原始异常，保留异常类型和调用堆栈，不将其吞掉。
                raise
        # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
        try:
            # 尝试原子发布首次解析清单；并发执行时只允许一份完整清单成为规范版本。
            try:os.link(temporary,target)
            # 其他执行者已经固定了解析结果，保留其文件并在下方重新读取，而不是覆盖。
            except FileExistsError:pass  # A concurrent owner already pinned the canonical parse.
        # 无论成功、失败或提前返回，都执行下方资源清理/统计逻辑。
        finally:
            # 清理解析清单临时文件，不删除已经发布的规范清单。
            temporary.unlink(missing_ok=True)
    # 拒绝符号链接和超限文件，避免归档读取跳转到非预期目标或占用过多内存。
    if target.is_symlink() or target.stat().st_size > 5 * 1024 * 1024:
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError('解析归档无效')
    # 读取原子发布的规范解析清单；并发生成者也使用同一份结果。
    record=json.loads(target.read_bytes())
    # 准备「待返回或归档的结构化数据」：从已取得的数据中读取对应字段/切片。
    payload=record['payload']
    # 准备「按稳定键顺序编码的 UTF-8 数据」：将字符串编码成字节以便哈希或字节数限制。
    encoded=json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()
    # 同时检查清单内容摘要与原文件哈希，防止损坏文件或错误归档混入重试。
    if (hashlib.sha256(encoded).hexdigest()!=record['digest'] or payload['sha256']!=job.sha256
            # 继续要求解析配置与文件名匹配当前任务，避免复用另一份任务的缓存。
            or payload['configuration']!=configuration or payload['filename']!=job.filename):
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError('解析归档哈希或配置不匹配')
    # 解析清单必须至少一块且不超过文档分块上限。
    if not 1 <= len(payload['chunks']) <= MAX_CHUNKS:
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError('解析归档分块数量无效')
    # 准备「最终可索引分块列表」：建立有序的数据列表。
    chunks=[]
    # 逐项处理当前 缓存清单中的分块，逐项重建边界校验。
    for item in payload['chunks']:
        # 逐块拒绝空正文与错误类型，读取归档时也重新建立数据边界。
        if (not isinstance(item['text'],str) or not item['text'].strip() or
                # 片段正文不得超过 Milvus 的 UTF-8 字节限制，页码还必须是整数。
                len(item['text'].encode())>4096 or type(item['page']) is not int or
                # 继续校验页码范围和章节路径类型，避免无效缓存发布为可检索数据。
                not 0 <= item['page'] <= 100 or not isinstance(item['section_path'],str)):
            # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
            raise ValueError('解析归档分块无效')
        # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
        chunks.append(KnowledgeChunk(**item))
    # 把校验后的缓存清单重新组装为 PreparedDocument，原文件仍来自已校验归档。
    return PreparedDocument(payload['filename'],payload['content_type'],payload['size_bytes'],job.sha256,chunks,raw)
