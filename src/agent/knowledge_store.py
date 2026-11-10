# 模块职责：管理 Milvus 文档向量；检索与发布以 PostgreSQL 的有效 revision 为准，避免返回旧版本。
"""Shared document vectors with PostgreSQL as the active-revision authority."""

# 导入 json，用于序列化结构化数据或构造安全的字符串字面量。
import json
# 导入 os，用于读取环境变量并执行必要的文件系统操作。
import os
# 导入 uuid，用于验证 UUID 或生成稳定/随机标识。
import uuid
# 导入 logging，用于记录模块运行状态与可排查的故障信息。
import logging
# 导入 time，用于以单调时钟统计耗时或安排轮询间隔。
import time
# 从 contextvars 导入 ContextVar，供下方隔离并发执行的检索统计。
from contextvars import ContextVar

# 准备「正式知识库集合名称」：设置为 'agent_knowledge_v1'。
KNOWLEDGE_COLLECTION = "agent_knowledge_v1"
# 准备「默认 COSINE 相似度过滤阈值」：设置为 0.4。
MIN_KNOWLEDGE_SCORE = 0.40  # Initial COSINE cutoff; tune against a fixed question set.
# 准备「当前模块日志记录器」：取得当前模块的日志对象。
logger = logging.getLogger(__name__)
# 使用 ContextVar 隔离每次执行的检索统计；它不是共享字典或线程锁。
last_retrieval = ContextVar('last_knowledge_retrieval', default=None)


# 定义类型：用专门异常表示候选窗口耗尽，防止将检索不完整当成无答案。
class KnowledgeRetrievalIncomplete(RuntimeError):
    # 该专门异常类型无需新增行为，仅用来让调用者区分这种失败情形。
    pass


# 定义函数：延迟取得共享 Milvus 客户端，避免导入模块时立刻连接外部服务。
def get_client():
    # 从 src.agent.memory.client 导入 get_client（别名 factory），供下方相关数据结构与业务步骤。
    from src.agent.memory.client import get_client as factory
    # 返回当前处理结果，由调用方继续决定下一步。
    return factory()


# 定义函数：延迟取得缓存的本地 Embedding 模型，避免重复加载模型权重。
def get_embed_model():
    # 从 src.agent.memory.client 导入 get_embed_model（别名 factory），供下方相关数据结构与业务步骤。
    from src.agent.memory.client import get_embed_model as factory
    # 返回当前处理结果，由调用方继续决定下一步。
    return factory()


# 定义函数：读取或接受显式相似度阈值，要求它可转换为 0 到 1 的数值。
def _score_threshold(value: float | None) -> float:
    # 准备「原始文件字节」：根据条件选择当前值或回退值。
    raw = os.getenv("KNOWLEDGE_MIN_SCORE", str(MIN_KNOWLEDGE_SCORE)) if value is None else value
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 准备「相似度分数」：转换成数值。
        score = float(raw)
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except (TypeError, ValueError) as exc:
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError("KNOWLEDGE_MIN_SCORE 必须是 0 到 1 之间的数字") from exc
    # COSINE 过滤阈值配置限定在 0 到 1；非法配置明确报错而不是静默改变质量门槛。
    if not 0 <= score <= 1:
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError("KNOWLEDGE_MIN_SCORE 必须是 0 到 1 之间的数字")
    # 返回通过范围校验的相似度阈值。
    return score


# 定义函数：只初始化正式知识库集合及 COSINE 索引，已有集合直接加载，不删除演示集合。
def ensure_knowledge_collection() -> None:
    # 函数约定：只初始化正式知识库集合及 COSINE 索引，已有集合直接加载，不删除演示集合。
    """Create only the new managed collection; never touch demo_collection."""
    # 从 pymilvus 导入 DataType，供下方相关数据结构与业务步骤。
    from pymilvus import DataType
    # 准备「client」：延迟取得共享 Milvus 客户端，避免导入模块时立刻连接外部服务。
    client = get_client()
    # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
    if client.has_collection(KNOWLEDGE_COLLECTION, timeout=10):
        # 加载已有/新建集合，使向量索引可用于检索。
        client.load_collection(KNOWLEDGE_COLLECTION, timeout=30)
        # 当前步骤已完成或无需继续，直接结束本函数。
        return
    # 创建显式字段结构并关闭自动 ID/动态字段，主键由 revision 与序号确定。
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    # 声明 Milvus 的「记录唯一 ID」字段，明确类型与长度/维度约束。
    schema.add_field(field_name="id", datatype=DataType.VARCHAR, is_primary=True, max_length=64)
    # 声明 Milvus 的「逻辑文档 ID」字段，明确类型与长度/维度约束。
    schema.add_field(field_name="document_id", datatype=DataType.VARCHAR, max_length=64)
    # 声明 Milvus 的「不可变文档版本」字段，明确类型与长度/维度约束。
    schema.add_field(field_name="revision", datatype=DataType.VARCHAR, max_length=64)
    # 声明 Milvus 的「分块顺序号」字段，明确类型与长度/维度约束。
    schema.add_field(field_name="chunk_index", datatype=DataType.INT64)
    # 声明 Milvus 的「原文件名称」字段，明确类型与长度/维度约束。
    schema.add_field(field_name="filename", datatype=DataType.VARCHAR, max_length=255)
    # 声明 Milvus 的「页码（无页码模型时为 0）」字段，明确类型与长度/维度约束。
    schema.add_field(field_name="page", datatype=DataType.INT64)
    # 声明 Milvus 的「片段原文」字段，明确类型与长度/维度约束。
    schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=4096)
    # 声明 Milvus 的「文本向量」字段，明确类型与长度/维度约束。
    schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=768)
    # 准备「indexes」：准备向量索引配置。
    indexes = client.prepare_index_params()
    # 为向量字段指定 COSINE 自动索引参数。
    indexes.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 创建正式集合与索引；不通过删库重建覆盖现有数据。
        client.create_collection(
            # 设置「Milvus 集合名」参数，使当前调用使用明确的配置或元数据。
            collection_name=KNOWLEDGE_COLLECTION, schema=schema, index_params=indexes, timeout=30,
        # 结束当前参数列表或结构化数据，后续语句继续处理这一结果。
        )
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except Exception:
        # Another API worker may have created the collection after our check.
        # 确认本步骤所需的输入、结果或资源是否缺失，缺失时走明确回退路径。
        if not client.has_collection(KNOWLEDGE_COLLECTION, timeout=10):
            # 继续传播原始异常，保留异常类型和调用堆栈，不将其吞掉。
            raise
    # 加载已有/新建集合，使向量索引可用于检索。
    client.load_collection(KNOWLEDGE_COLLECTION, timeout=30)


# 定义函数：按 UTF-8 字节而非字符截断字符串，忽略尾部不完整编码以满足 Milvus 长度约束。
def _milvus_string(value: str, max_bytes: int) -> str:
    # 返回不超过指定 UTF-8 字节数的合法字符串。
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


# 定义函数：按 32 块分批生成向量并幂等 upsert；成功写向量还不等于发布有效文档版本。
def index_document(document_id: uuid.UUID, revision: uuid.UUID, filename: str, chunks) -> int:
    # 函数约定：按 32 块分批生成向量并幂等 upsert；成功写向量还不等于发布有效文档版本。
    """Insert a new immutable revision; caller switches the DB revision after success."""
    # 只初始化正式知识库集合及 COSINE 索引，已有集合直接加载，不删除演示集合。
    ensure_knowledge_collection()
    # 准备「client」：延迟取得共享 Milvus 客户端，避免导入模块时立刻连接外部服务。
    client = get_client()
    # 准备「embedding」：延迟取得缓存的本地 Embedding 模型，避免重复加载模型权重。
    embedding = get_embed_model()
    # 逐项处理当前 批次中的分块与对应向量。
    for start in range(0, len(chunks), 32):
        # 准备「当前一批分块或元数据」：从已取得的数据中读取对应字段/切片。
        batch = chunks[start:start + 32]
        # 准备「vectors」：批量编码文档正文。
        vectors = embedding.embed_documents([chunk.text for chunk in batch])
        # 每个文本块必须对应一个向量，数量不一致就停止，避免 zip 静默丢掉数据。
        if len(vectors) != len(batch):
            # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
            raise RuntimeError("Embedding 数量与文档分块不一致")
        # 准备「entities」：按条件逐项转换、过滤或整理数据。
        entities = [
            # 开始为当前分块构造 Milvus 实体，下面的主键与元数据必须对应同一文本/向量。
            {
                # 写入「记录唯一 ID」字段，与同一条记录的其他字段共同组成结果。
                "id": str(uuid.uuid5(uuid.UUID(str(revision)), str(start + offset))),
                # 写入「逻辑文档 ID」字段，与同一条记录的其他字段共同组成结果。
                "document_id": str(document_id),
                # 写入「不可变文档版本」字段，与同一条记录的其他字段共同组成结果。
                "revision": str(revision),
                # 写入「分块顺序号」字段，与同一条记录的其他字段共同组成结果。
                "chunk_index": start + offset,
                # 写入「原文件名称」字段，与同一条记录的其他字段共同组成结果。
                "filename": _milvus_string(filename, 255),
                # 写入「页码（无页码模型时为 0）」字段，与同一条记录的其他字段共同组成结果。
                "page": chunk.page,
                # 写入「片段原文」字段，与同一条记录的其他字段共同组成结果。
                "text": chunk.text,
                # 写入「文本向量」字段，与同一条记录的其他字段共同组成结果。
                "vector": vector,
            # 结束当前参数列表或结构化数据，后续语句继续处理这一结果。
            }
            # 逐项处理当前 批次中的分块与对应向量。
            for offset, (chunk, vector) in enumerate(zip(batch, vectors))
        # 结束当前参数列表或结构化数据，后续语句继续处理这一结果。
        ]
        # 按确定性主键幂等写向量；同一任务重试更新原键而不是新增重复片段。
        client.upsert(collection_name=KNOWLEDGE_COLLECTION, data=entities, timeout=30)
    # 返回实际处理的分块数；SQL 有效版本尚需由发布事务切换。
    return len(chunks)


# 定义函数：按文档和可选 revision 构造安全过滤表达式，删除相应 Milvus 片段。
def delete_document_chunks(document_id: uuid.UUID, revision: uuid.UUID | None = None) -> None:
    # 准备「client」：延迟取得共享 Milvus 客户端，避免导入模块时立刻连接外部服务。
    client = get_client()
    # 确认本步骤所需的输入、结果或资源是否缺失，缺失时走明确回退路径。
    if not client.has_collection(KNOWLEDGE_COLLECTION, timeout=10):
        # 当前步骤已完成或无需继续，直接结束本函数。
        return
    # 准备「expression」：整理当前步骤需要的数据。
    expression = f"document_id == {json.dumps(str(document_id))}"
    # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
    if revision is not None:
        # 累计更新「expression」，供当前步骤的计数或边界判断使用。
        expression += f" && revision == {json.dumps(str(revision))}"
    # 删除已通过文档/修订过滤约束的向量。
    client.delete(collection_name=KNOWLEDGE_COLLECTION, filter=expression, timeout=30)


# 定义函数：从 SQL 权威目录读取未删除文档的当前版本与名称，并记录查询耗时。
def _active_revisions(document_ids: set[str]) -> dict[str, tuple[str, str]]:
    # 函数约定：从 SQL 权威目录读取未删除文档的当前版本与名称，并记录查询耗时。
    """A candidate is usable only after its metadata transaction has committed."""
    # 确认本步骤所需的输入、结果或资源是否缺失，缺失时走明确回退路径。
    if not document_ids:
        # 没有可用输入或结果，返回空容器；上层据此区分无结果和实际异常。
        return {}
    # 从 src.agent.knowledge_database 导入 knowledge_session，供下方相关数据结构与业务步骤。
    from src.agent.knowledge_database import knowledge_session
    # 从 sqlalchemy 导入 select，供下方构造数据库字段与参数化查询。
    from sqlalchemy import select
    # 从 api.models 导入 KnowledgeDocument，供下方相关数据结构与业务步骤。
    from api.models import KnowledgeDocument

    # 准备「待查询的文档 UUID 集合」：按条件逐项转换、过滤或整理数据。
    ids = [uuid.UUID(value) for value in document_ids]
    # 准备「单调时钟计时起点」：读取不受系统时钟校准影响的计时值。
    started = time.monotonic()
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 借用同步数据库会话，退出上下文时释放连接，不长期占用事务。
        with knowledge_session() as session:
            # 准备「数据库行或文档表格行」：取出本次有界查询的全部返回行。
            rows = session.execute(select(KnowledgeDocument.id, KnowledgeDocument.revision, KnowledgeDocument.filename)
                # 继续组合本次操作的参数、条件或结果字段。
                .where(KnowledgeDocument.deleted_at.is_(None), KnowledgeDocument.id.in_(ids))).all()
            # 把有效文档 ID 映射到当前 revision 和文件名，为候选过滤提供权威依据。
            return {str(doc_id): (str(revision), filename) for doc_id, revision, filename in rows}
    # 无论成功、失败或提前返回，都执行下方资源清理/统计逻辑。
    finally:
        # 准备「本次检索的统计记录」：读取映射值，并在字段缺失时采用指定默认值。
        trace = last_retrieval.get()
        # 当前查询有统计对象才追加指标；统计不会跨请求共享。
        if trace is not None:
            # 准备「trace['metadata_queries']」：按当前配置合并、累计或计算该值。
            trace['metadata_queries'] = trace.get('metadata_queries', 0) + 1
            # 准备「trace['metadata_ms']」：调用 round 完成当前步骤。
            trace['metadata_ms'] = round(trace.get('metadata_ms', 0) + (time.monotonic() - started) * 1000, 2)


# 定义函数：按 COSINE 取候选，再过滤非有效修订；不足时扩大窗口，但不把窗口耗尽伪装成无答案。
def _search_vector_chunks(
    # 声明「检索问题/当前 SQL 查询」输入的类型、默认值或依赖来源，供调用方/API 参数校验使用。
    query: str, limit: int = 5, min_score: float | None = None,
# 结束函数参数与返回类型声明，下面进入处理正文。
) -> list[dict]:
    # 函数约定：按 COSINE 取候选，再过滤非有效修订；不足时扩大窗口，但不把窗口耗尽伪装成无答案。
    """Return only live chunks from the shared catalog, in COSINE score order."""
    # 空白问题不连接检索服务，直接返回空结果。
    if not query.strip():
        # 没有可用输入或结果，返回空容器；上层据此区分无结果和实际异常。
        return []
    # 先限制每次请求的结果数量，避免任意放大检索资源消耗。
    if not 1 <= limit <= 20:
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError("知识库检索数量无效")
    # 准备「生效的 COSINE 阈值」：读取或接受显式相似度阈值，要求它可转换为 0 到 1 的数值。
    threshold = _score_threshold(min_score)
    # 准备「client」：延迟取得共享 Milvus 客户端，避免导入模块时立刻连接外部服务。
    client = get_client()
    # 确认本步骤所需的输入、结果或资源是否缺失，缺失时走明确回退路径。
    if not client.has_collection(KNOWLEDGE_COLLECTION, timeout=10):
        # 没有可用输入或结果，返回空容器；上层据此区分无结果和实际异常。
        return []
    # 把问题前 2000 字符编码为向量，限制输入规模；向量相似不等于有事实答案。
    vector = get_embed_model().embed_query(query[:2000])
    # 准备「当前候选窗口大小」：选取较大值以保证最小窗口或有效分母。
    search_limit = max(20, limit * 5)
    # 持续处理下一轮任务、候选窗口或盘点批次，直到内部退出条件成立。
    while True:
        # 准备「本次检索的统计记录」：读取映射值，并在字段缺失时采用指定默认值。
        trace = last_retrieval.get()
        # 当前查询有统计对象才追加指标；统计不会跨请求共享。
        if trace is not None:
            # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
            trace['windows'].append(search_limit)
        # 准备「results」：调用 client.search 完成当前步骤。
        results = client.search(
            # 设置「Milvus 集合名」参数，使当前调用使用明确的配置或元数据。
            collection_name=KNOWLEDGE_COLLECTION,
            # 设置「写入或查询用的数据」参数，使当前调用使用明确的配置或元数据。
            data=[vector], anns_field="vector",
            # 设置「相似度检索参数」参数，使当前调用使用明确的配置或元数据。
            search_params={"metric_type": "COSINE"},
            # 设置「结果/候选数量上限」参数，使当前调用使用明确的配置或元数据。
            limit=search_limit,
            # 设置「需要返回的元数据字段」参数，使当前调用使用明确的配置或元数据。
            output_fields=["document_id", "revision", "chunk_index", "filename", "page", "text"],
            # 设置「读取一致性级别」参数，使当前调用使用明确的配置或元数据。
            consistency_level="Strong",
            # 设置「等待/执行超时秒数」参数，使当前调用使用明确的配置或元数据。
            timeout=10,
        # 结束当前参数列表或结构化数据，后续语句继续处理这一结果。
        )
        # 准备「当前返回的检索候选」：根据条件选择当前值或回退值。
        hits = results[0] if results else []
        # 准备「满足初步条件的候选」：按条件逐项转换、过滤或整理数据。
        candidates = [hit for hit in hits if float(hit.get("distance", -1)) >= threshold]
        # 当前查询有统计对象才追加指标；统计不会跨请求共享。
        if trace is not None:
            # 累计更新「trace['candidates_examined']」，供当前步骤的计数或边界判断使用。
            trace['candidates_examined'] += len(hits)
        # 准备「文档当前有效版本映射」：从 SQL 权威目录读取未删除文档的当前版本与名称，并记录查询耗时。
        active = _active_revisions({hit["entity"]["document_id"] for hit in candidates})
        # 准备「最终筛选结果」：建立有序的数据列表。
        found = []
        # 逐项处理当前 候选数据。
        for hit in candidates:
            # 准备「Milvus 命中的业务字段」：从已取得的数据中读取对应字段/切片。
            entity = hit["entity"]
            # 准备「当前候选对应的文档 ID」：从已取得的数据中读取对应字段/切片。
            doc_id = entity["document_id"]
            # 准备「当前锁定的数据库记录/当前分块」：读取映射值，并在字段缺失时采用指定默认值。
            current = active.get(doc_id)
            # SQL 中不存在有效文档或 revision 已变化时丢弃候选，屏蔽旧版本、已删除和未发布向量。
            if current is None or current[0] != entity["revision"]:
                # 当前查询有统计对象才追加指标；统计不会跨请求共享。
                if trace is not None:
                    # 累计更新「trace['inactive_filtered']」，供当前步骤的计数或边界判断使用。
                    trace['inactive_filtered'] += 1
                # 丢弃旧修订或不合法正文，继续检查下一条向量候选。
                continue
            # 候选必须具有可显示的文档名和非空字符串正文，才可交给回答模型。
            if not current[1].strip() or not isinstance(entity["text"], str) or not entity["text"].strip():
                # 丢弃旧修订或不合法正文，继续检查下一条向量候选。
                continue
            # 准备「页码（无页码模型时为 0）」：转换成整数。
            page = int(entity["page"])
            # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
            found.append({
                # 写入「逻辑文档 ID」字段，与同一条记录的其他字段共同组成结果。
                "document_id": doc_id,
                # 写入「不可变文档版本」字段，与同一条记录的其他字段共同组成结果。
                "revision": entity["revision"],
                # 写入「片段 ID」字段，与同一条记录的其他字段共同组成结果。
                "chunk_id": str(hit["id"]),
                # 写入「title」字段，与同一条记录的其他字段共同组成结果。
                "title": current[1],
                # 写入「页码（无页码模型时为 0）」字段，与同一条记录的其他字段共同组成结果。
                "page": page if page > 0 else None,
                # 写入「片段原文」字段，与同一条记录的其他字段共同组成结果。
                "text": entity["text"],
                # 写入「相似度分数」字段；这是显式元数据，不直接表示最终答案的正确性。
                "score": round(float(hit["distance"]), 4),
            # 结束当前参数列表或结构化数据，后续语句继续处理这一结果。
            })
            # 检查数量/长度是否达到边界，防止越界或超预算处理。
            if len(found) >= limit:
                # 返回通过分数与有效版本校验的候选，而不是未经筛选的 Milvus 原始结果。
                return found
        # Old revisions and incomplete writes are filtered by PostgreSQL.
        # Widen the candidate window only while a better live hit may remain.
        # 候选已经取尽、低分边界已出现或达到最大窗口时，停止继续扩张。
        if len(hits) < search_limit or len(candidates) < len(hits) or search_limit >= 1000:
            # 只有取满上限且候选都在阈值内，才标记仍可能有有效片段被挡在窗口外。
            if search_limit >= 1000 and len(hits) >= 1000 and len(candidates) == len(hits):
                # 当前查询有统计对象才追加指标；统计不会跨请求共享。
                if trace is not None:
                    # 记录窗口已达到 1000 条上限，供故障排查区分无答案与旧向量拥堵。
                    trace['candidate_cap_reached'] = True
                # 确认本步骤所需的输入、结果或资源是否缺失，缺失时走明确回退路径。
                if not found:
                    # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
                    raise KnowledgeRetrievalIncomplete('知识库候选窗口耗尽，无法确认无答案；请核对并清理无效修订')
            # 返回通过分数与有效版本校验的候选，而不是未经筛选的 Milvus 原始结果。
            return found
        # 把下一轮候选窗口放大 5 倍，最多 1000 条，避免旧修订过多时无限扫描。
        search_limit = min(1000, search_limit * 5)


# 定义函数：统一选择向量或混合检索，可选扩展相邻片段，并保留本次查询的统计信息。
def search_knowledge_chunks(query: str, limit: int = 5, min_score: float | None = None) -> list[dict]:
    # 先限制每次请求的结果数量，避免任意放大检索资源消耗。
    if not 1 <= limit <= 20:
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError('知识库检索数量无效')
    # 准备「当前检索/权限策略模式」：读取环境配置（未设置时采用代码中的默认值）。
    mode = os.getenv('KNOWLEDGE_RETRIEVAL_MODE', 'vector')
    # 拒绝未知检索策略名称，避免操作员拼写错误后悄悄执行另一种策略。
    if mode not in {'vector', 'hybrid'}:
        # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
        raise ValueError('不支持的知识库检索模式')
    # 准备「本次检索的统计记录」：组织该步骤需要的结构化字段。
    trace = {'mode': mode, 'windows': [], 'candidates_examined': 0, 'inactive_filtered': 0}
    # 把本次统计绑定到当前执行上下文，避免并发请求共用一个可变全局统计对象。
    last_retrieval.set(trace)
    # 准备「单调时钟计时起点」：读取不受系统时钟校准影响的计时值。
    started = time.monotonic()
    # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
    try:
        # 开始受保护的操作区；失败时转入配对的 except，收尾由 finally 保证执行。
        try:
            # 准备「文本向量」：按 COSINE 取候选，再过滤非有效修订；不足时扩大窗口，但不把窗口耗尽伪装成无答案。
            vector = _search_vector_chunks(query, limit if mode == 'vector' else 20, min_score)
        # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
        except KnowledgeRetrievalIncomplete:
            # 根据已校验的检索或权限模式执行对应分支。
            if mode != 'hybrid':
                # 继续传播原始异常，保留异常类型和调用堆栈，不将其吞掉。
                raise
            # 准备「文本向量」：建立有序的数据列表。
            vector = []
            # 标记向量一路不完整；混合模式仍可尝试从有效 SQL 关键词目录恢复结果。
            trace['vector_incomplete'] = True
        # 空白问题不连接检索服务，直接返回空结果。
        if not query.strip():
            # 没有可用输入或结果，返回空容器；上层据此区分无结果和实际异常。
            return []
        # 先限制每次请求的结果数量，避免任意放大检索资源消耗。
        if not 1 <= limit <= 20:
            # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
            raise ValueError('知识库检索数量无效')
        # 根据已校验的检索或权限模式执行对应分支。
        if mode == 'hybrid':
            # 从 src.agent.knowledge_catalog 导入 lexical_candidates，供下方相关数据结构与业务步骤。
            from src.agent.knowledge_catalog import lexical_candidates
            # 从 src.agent.knowledge_ranking 导入 fuse_candidates，供下方相关数据结构与业务步骤。
            from src.agent.knowledge_ranking import fuse_candidates
            # 准备「关键词查询计时起点」：读取不受系统时钟校准影响的计时值。
            lexical_started = time.monotonic()
            # 准备「关键词召回候选」：先用有效版本和关键词索引筛选，再按词项权重排序，最后限制候选数量。
            lexical = lexical_candidates(query)
            # 准备「trace['lexical_ms']」：调用 round 完成当前步骤。
            trace['lexical_ms'] = round((time.monotonic() - lexical_started) * 1000, 2)
            # 准备「trace['lexical_candidates']」：读取数量或长度。
            trace['lexical_candidates'] = len(lexical)
            # 准备「最终筛选结果」：融合向量与关键词排名，优先精确标识符，再去重并控制每份文档的结果配额。
            found = fuse_candidates(query, vector, lexical, limit=limit)
            # Fusion/lexical retrieval never bypasses the active-revision authority.
            # 准备「文档当前有效版本映射」：从 SQL 权威目录读取未删除文档的当前版本与名称，并记录查询耗时。
            active = _active_revisions({h['document_id'] for h in found})
            # 融合后再次核对有效 revision；关键词或排序流程不能绕过 SQL 权威目录。
            found = [h for h in found if active.get(h['document_id'], (None,))[0] == h.get('revision')]
            # 向量一路耗尽且关键词也没恢复结果时报告检索不完整，不伪报正确的无答案。
            if not found and trace.get('vector_incomplete'):
                # 停止当前不安全或不完整的处理，将异常交给上层统一记录与回滚。
                raise KnowledgeRetrievalIncomplete('向量候选窗口耗尽，关键词检索也未能确认答案')
        # 进入前一条件未满足时的分支，采用对应的回退处理。
        else:
            # 准备「最终筛选结果」：使用文本向量。
            found = vector
        # 检查本分支的前置条件；只有条件成立才执行下方处理，其他情况保留原有状态。
        if os.getenv('KNOWLEDGE_EXPAND_NEIGHBORS') == '1':
            # 从 src.agent.knowledge_catalog 导入 expand_neighbors，供下方相关数据结构与业务步骤。
            from src.agent.knowledge_catalog import expand_neighbors
            # 准备「最终筛选结果」：只从当前有效修订读取前后相邻块，同时控制正文预算与单片段字节上限。
            found = expand_neighbors(found)
        # 准备「trace['returned']」：读取数量或长度。
        trace['returned'] = len(found)
        # 返回最终策略选出的片段；finally 仍会记录本次检索耗时。
        return found
    # 捕获此处声明的错误，按下方分支转换成领域错误、HTTP 状态或安全的失败结果。
    except Exception:
        # 准备「trace['failed']」：将该状态明确标记为真。
        trace['failed'] = True
        # 继续传播原始异常，保留异常类型和调用堆栈，不将其吞掉。
        raise
    # 无论成功、失败或提前返回，都执行下方资源清理/统计逻辑。
    finally:
        # 准备「trace['duration_ms']」：调用 round 完成当前步骤。
        trace['duration_ms'] = round((time.monotonic() - started) * 1000, 2)
        # 记录当前阶段的状态/故障或统计，便于排查；这里不输出完整上传正文。
        logger.info('knowledge_retrieval %s', json.dumps(trace, ensure_ascii=False))
