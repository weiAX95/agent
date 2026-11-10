# 模块职责：保存 SQL 分块目录，按有效文档版本召回关键词候选，并在预算内扩展相邻片段。
"""Revision-authoritative SQL chunk catalog and indexed lexical candidates."""
# 导入 uuid，用于验证 UUID 或生成稳定/随机标识。
import uuid
# 从 sqlalchemy 导入 Text, cast, delete, select，供下方构造数据库字段与参数化查询。
from sqlalchemy import Text, cast, delete, select
# 从 sqlalchemy.dialects.postgresql 导入 ARRAY，供下方使用 PostgreSQL 特定类型。
from sqlalchemy.dialects.postgresql import ARRAY
# 从 api.knowledge_models 导入 KnowledgeChunkRecord，供下方相关数据结构与业务步骤。
from api.knowledge_models import KnowledgeChunkRecord
# 从 api.models 导入 KnowledgeDocument，供下方相关数据结构与业务步骤。
from api.models import KnowledgeDocument
# 从 src.agent.knowledge_ranking 导入 query_tokens, lexical_score，供下方相关数据结构与业务步骤。
from src.agent.knowledge_ranking import query_tokens, lexical_score


# 定义函数：用 revision 作为 UUID 命名空间、块序号作为名字，生成重试不变的片段 ID。
def chunk_uuid(revision, index):
    # 由固定命名空间和分块序号得到稳定 UUID，同一 revision 重试不会产生另一组随机片段 ID。
    return uuid.uuid5(uuid.UUID(str(revision)), str(index))


# 定义函数：把解析后的分块转换成 SQL 目录对象，同时生成可由 GIN 索引检索的 token。
def chunk_records(document_id, revision, chunks):
    # 为每个分块生成目录对象；ID 与向量端采用同一 revision/序号算法。
    return [KnowledgeChunkRecord(id=chunk_uuid(revision, index), document_id=document_id,
            # 设置「不可变文档版本」参数，使当前调用使用明确的配置或元数据。
            revision=revision, chunk_index=index, text=chunk.text, page=chunk.page,
            # 设置「章节路径」参数，使当前调用使用明确的配置或元数据。
            section_path=chunk.section_path, search_tokens=query_tokens(chunk.text, max_tokens=4096))
            # 逐项处理当前 候选数据。
            for index, chunk in enumerate(chunks)]


# 定义函数：在调用方事务中替换指定修订的 SQL 目录；此处不自行提交事务。
def replace_chunk_catalog(session, document_id, revision, chunks):
    # 执行当前参数化 SQL 语句；事务边界仍由外层上下文管理。
    session.execute(delete(KnowledgeChunkRecord).where(KnowledgeChunkRecord.document_id == document_id,
                                                      # 把目录删除范围进一步限制到这一 revision，保留同文档的其他版本。
                                                      KnowledgeChunkRecord.revision == revision))
    # 把这一批目录对象加入同一数据库事务，等待外层统一提交。
    session.add_all(chunk_records(document_id, revision, chunks))


# 定义函数：先用有效版本和关键词索引筛选，再按词项权重排序，最后限制候选数量。
def lexical_candidates(query, limit=100):
    # 从 src.agent.knowledge_database 导入 knowledge_session，供下方相关数据结构与业务步骤。
    from src.agent.knowledge_database import knowledge_session
    # 准备「去重后的查询关键词」：保留英文/数字标识符，并把连续中文拆成双字片段，去重后限制数量。
    tokens = query_tokens(query)
    # 确认本步骤所需的输入、结果或资源是否缺失，缺失时走明确回退路径。
    if not tokens:
        # 没有可用输入或结果，返回空容器；上层据此区分无结果和实际异常。
        return []
    # 借用同步数据库会话，退出上下文时释放连接，不长期占用事务。
    with knowledge_session() as session:
        # 准备「尚未执行的 SQL 查询」：为 SQL 语句添加过滤条件。
        statement = select(KnowledgeChunkRecord, KnowledgeDocument.filename).join(
            # 通过文档 ID 将分块连接到权威文档目录，接着还必须核对 revision。
            KnowledgeDocument, (KnowledgeDocument.id == KnowledgeChunkRecord.document_id) &
            # 要求分块 revision 等于文档当前有效 revision，旧分块不能参与本次结果。
            (KnowledgeDocument.revision == KnowledgeChunkRecord.revision)).where(
            # 只保留尚未软删除的文档。
            KnowledgeDocument.deleted_at.is_(None),
            # 使用 PostgreSQL JSONB 的 ?| 操作符匹配任一查询 token；右侧为参数化文本数组，可利用 GIN 索引。
            KnowledgeChunkRecord.search_tokens.op('?|')(cast(tokens, ARRAY(Text))))
        # Rank before LIMIT, so matching but weak rows cannot crowd out exact identifiers.
        # 准备「关键词命中权重」：按条件逐项转换、过滤或整理数据。
        weights = [(3 if any(c.isascii() and c.isalnum() for c in t) else 1) for t in tokens]
        # 从 sqlalchemy 导入 case，供下方构造数据库字段与参数化查询。
        from sqlalchemy import case
        # 准备「当前排名/SQL 排序权重」：累计匹配权重或总量。
        rank = sum(case((KnowledgeChunkRecord.search_tokens.op('?')(t), w), else_=0)
                   # 逐项处理当前 候选数据。
                   for t, w in zip(tokens, weights))
        # 在数据库中先按匹配权重排序再取 Top 候选，同分时用块 ID 保证稳定顺序。
        rows = session.execute(statement.order_by(rank.desc(), KnowledgeChunkRecord.id).limit(limit)).all()
        # 将有效 SQL 行转换成统一检索格式；关键词分数不冒充 COSINE，所以原始 score 为 None。
        return [dict(document_id=str(row.document_id), revision=str(row.revision), chunk_id=str(row.id),
                     # 设置「title」参数，使当前调用使用明确的配置或元数据。
                     title=filename, page=row.page or None, text=row.text,
                     # 设置「章节路径」参数，使当前调用使用明确的配置或元数据。
                     section_path=row.section_path, chunk_index=row.chunk_index,
                     # 设置「相似度分数」参数，使当前调用使用明确的配置或元数据。
                     score=None, score_kind='lexical') for row, filename in rows]


# 定义函数：只从当前有效修订读取前后相邻块，同时控制正文预算与单片段字节上限。
def expand_neighbors(hits, *, budget=4000):
    # 函数约定：只从当前有效修订读取前后相邻块，同时控制正文预算与单片段字节上限。
    """Fetch only currently active revisions; never use an arbitrary historical chunk ID."""
    # 从 src.agent.knowledge_database 导入 knowledge_session，供下方相关数据结构与业务步骤。
    from src.agent.knowledge_database import knowledge_session
    # 确认本步骤所需的输入、结果或资源是否缺失，缺失时走明确回退路径。
    if not hits:
        # 没有可用输入或结果，返回空容器；上层据此区分无结果和实际异常。
        return []
    # 准备「(expanded, used)」：把本步骤相关值放在同一有序组合中。
    expanded, used = [], 0
    # 借用同步数据库会话，退出上下文时释放连接，不长期占用事务。
    with knowledge_session() as session:
        # 逐项处理当前 命中及其同修订相邻块。
        for hit in hits:
            # 先保证主命中正文也放得进整体字符预算，避免相邻扩展无界增长。
            if used + len(hit['text']) > budget:
                # 跳过失效、超预算或当前块本身，不把它作为额外相邻正文加入。
                continue
            # 准备「当前数据库行/表格行」：执行查询并取得一个对象/标量。
            row = session.scalar(select(KnowledgeChunkRecord).join(KnowledgeDocument,
                # 要求分块属于当前文档，再与下行的版本相等条件共同构成连接。
                (KnowledgeDocument.id == KnowledgeChunkRecord.document_id) &
                # 要求分块 revision 等于文档当前有效 revision，旧分块不能参与本次结果。
                (KnowledgeDocument.revision == KnowledgeChunkRecord.revision)).where(
                # 只查未删除文档中的这条片段；UUID 校验也防止用任意字符串作为目录标识。
                KnowledgeDocument.deleted_at.is_(None), KnowledgeChunkRecord.id == uuid.UUID(hit['chunk_id'])))
            # 处理没有 SQL 分块旁表的老向量，仍须回查文档有效版本。
            if row is None:
                # 准备「authority」：读取映射值，并在字段缺失时采用指定默认值。
                authority = session.get(KnowledgeDocument, uuid.UUID(hit['document_id']))
                # 老向量即使没有 SQL 分块旁表，也必须核对文档存在、未删除且 revision 仍有效。
                if authority is None or authority.deleted_at is not None or str(authority.revision) != hit.get('revision'):
                    # 跳过失效、超预算或当前块本身，不把它作为额外相邻正文加入。
                    continue
                # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
                expanded.append(hit)  # Legacy vectors may have no SQL sidecar.
                # 累计更新「已经占用的正文字符预算」，供当前步骤的计数或边界判断使用。
                used += len(hit['text'])
                # 跳过失效、超预算或当前块本身，不把它作为额外相邻正文加入。
                continue
            # 准备「同一修订中的相邻分块」：取出本次有界查询的全部返回行。
            neighbors = session.scalars(select(KnowledgeChunkRecord).where(
                # 相邻扩展只允许读取同一文档。
                KnowledgeChunkRecord.document_id == row.document_id,
                # 继续限制为同一有效修订，不能跨版本拼接证据。
                KnowledgeChunkRecord.revision == row.revision,
                # 只取当前块前一块、当前块和后一块，避免把整份文档无界加入上下文。
                KnowledgeChunkRecord.chunk_index.between(row.chunk_index - 1, row.chunk_index + 1))
                # 继续组合同文档同 revision 的相邻范围和页码元数据。
                .order_by(KnowledgeChunkRecord.chunk_index)).all()
            # 准备「片段原文」：从已取得的数据中读取对应字段/切片。
            text = hit['text']
            # 准备「合并证据涉及的页码」：建立有序的数据列表。
            pages = [row.page]
            # 逐项处理当前 命中及其同修订相邻块。
            for neighbor in neighbors:
                # 当前块已经在正文中，扩展时不重复加入它自己。
                if neighbor.id == row.id:
                    # 跳过失效、超预算或当前块本身，不把它作为额外相邻正文加入。
                    continue
                # 准备「尝试合并后的片段正文」：根据条件选择当前值或回退值。
                candidate = (neighbor.text + '\n\n' + text if neighbor.chunk_index < row.chunk_index
                             # 继续组合同文档同 revision 的相邻范围和页码元数据。
                             else text + '\n\n' + neighbor.text)
                # 只有同时满足单片段字节上限和整体字符预算，才接受相邻正文合并。
                if len(candidate.encode('utf-8')) <= 4096 and used + len(candidate) <= budget:
                    # 准备「片段原文」：使用尝试合并后的片段正文。
                    text = candidate
                    # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
                    pages.append(neighbor.page)
            # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
            expanded.append({**hit, 'text': text, 'page': min(pages) or None,
                             # 写入「证据结束页码」字段，与同一条记录的其他字段共同组成结果。
                             'page_end': max(pages) or None, 'section_path': row.section_path})
            # 累计更新「已经占用的正文字符预算」，供当前步骤的计数或边界判断使用。
            used += len(text)
    # 返回预算内的相邻扩展结果，保留来源信息和页码范围。
    return expanded
