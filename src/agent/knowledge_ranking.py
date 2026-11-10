# 模块职责：提取中文双字词与英文标识符，用倒数排名融合两路候选，并控制重复片段和单文档占比。
"""Deterministic lexical features and reciprocal-rank fusion; no extra models."""
# 导入 re，用于识别关键词、标题、引用编号或合法配置值。
import re


# 定义函数：保留英文/数字标识符，并把连续中文拆成双字片段，去重后限制数量。
def query_tokens(query: str, max_tokens=64) -> list[str]:
    # 准备「去重后的查询关键词」：一次找出全部符合模式的文本片段。
    tokens = re.findall(r'[a-z0-9]+(?:[-_.][a-z0-9]+)*', query.lower())
    # 逐项处理当前 连续中文段，拆出可检索的双字 token。
    for phrase in re.findall(r'[\u3400-\u9fff]+', query):
        # 为连续中文追加双字片段；单字输入也保留，便于建立不依赖分词服务的词项。
        tokens.extend(phrase[i:i + 2] for i in range(max(1, len(phrase) - 1)))
    # 按首次出现顺序去重，并限制查询或分块 token 数量，避免 SQL 条件无限扩张。
    return list(dict.fromkeys(tokens))[:max_tokens]


# 定义函数：计算查询 token 在文本中的加权覆盖，作为确定性排序特征而非置信概率。
def lexical_score(query, text):
    # 准备「去重后的查询关键词」：保留英文/数字标识符，并把连续中文拆成双字片段，去重后限制数量。
    tokens = query_tokens(query)
    # 准备「待匹配或保存的正文」：统一小写以便匹配。
    content = text.lower()
    # 返回加权词项覆盖值；该值用于相对排序，不是概率或答案可信度。
    return sum((3 if re.search('[a-z0-9]', token) else 1) for token in tokens if token in content) / max(1, len(tokens))


# 定义函数：融合向量与关键词排名，优先精确标识符，再去重并控制每份文档的结果配额。
def fuse_candidates(query, vector, lexical, *, limit=5, per_document=3):
    # 准备「(scores, items)」：把本步骤相关值放在同一有序组合中。
    scores, items = {}, {}
    # 逐项处理当前 召回排名或融合后的候选。
    for ranking in (vector, sorted(lexical, key=lambda h: lexical_score(query, h['text']), reverse=True)):
        # 准备「当前一路召回已经计分的片段键」：建立去重集合。
        seen = set()
        # 逐项处理当前 召回排名或融合后的候选。
        for rank, item in enumerate(ranking, 1):
            # 准备「用于去重的文档/片段组合键」：把本步骤相关值放在同一有序组合中。
            key = (item['document_id'], item['chunk_id'])
            # 同一路排名里同一片段只计分一次。
            if key in seen:
                # 跳过当前一路重复或违反结果配额的候选，继续考察后续结果。
                continue
            # 把该项加入去重集合或当前数据库事务。
            seen.add(key)
            # 第一次遇到此片段时保存其元数据，后续重复召回只累加排名贡献。
            items.setdefault(key, item)
            # 为该候选累加 RRF 分数 1/(60+排名)，融合两路次序而不直接混用不可比的原始分数。
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    # Exact identifiers receive a deterministic boost, not a learned semantic score.
    # 准备「查询中的英文/数字标识 token」：按条件逐项转换、过滤或整理数据。
    identifiers = [t for t in query_tokens(query) if re.search('[a-z0-9]', t)]
    # 准备「融合排序后的候选键」：按指定特征建立稳定顺序。
    ordered = sorted(items, key=lambda k: (sum(t in items[k]['text'].lower() for t in identifiers),
                                            # 标识符命中相同后，按 RRF 分数和词项覆盖从高到低排序。
                                            scores[k], lexical_score(query, items[k]['text'])), reverse=True)
    # 准备「(found, fingerprints, counts)」：把本步骤相关值放在同一有序组合中。
    found, fingerprints, counts = [], set(), {}
    # 逐项处理当前 召回排名或融合后的候选。
    for key in ordered:
        # 准备「item」：从已取得的数据中读取对应字段/切片。
        item = items[key]
        # 准备「去除空白后的片段指纹」：按指定分隔符合并数据。
        fingerprint = ''.join(item['text'].split())
        # 准备「当前文档对象/文档标识」：从已取得的数据中读取对应字段/切片。
        doc = item['document_id']
        # 跳过重复正文或已达到单文档配额的候选，防止相似片段占满全部结果。
        if fingerprint in fingerprints or counts.get(doc, 0) >= per_document:
            # 跳过当前一路重复或违反结果配额的候选，继续考察后续结果。
            continue
        # 把该项加入去重集合或当前数据库事务。
        fingerprints.add(fingerprint)
        # 准备「counts[doc]」：按当前配置合并、累计或计算该值。
        counts[doc] = counts.get(doc, 0) + 1
        # 把当前已处理的数据加入结果/缓冲列表，供后续步骤统一使用。
        found.append({**item, 'ranking_score': round(scores[key], 6), 'retrieval_mode':'hybrid'})
        # 检查数量/长度是否达到边界，防止越界或超预算处理。
        if len(found) >= limit:
            # 结束当前循环：已经达到结果上限，或已经没有下一批可读取数据。
            break
    # 返回已经融合排序、去重并满足单文档配额的结果。
    return found
