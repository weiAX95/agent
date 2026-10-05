import json
import os
import uuid

import requests
from langchain_core.tools import tool
from langchain_huggingface import HuggingFaceEmbeddings
from pymilvus import DataType, MilvusClient

model_path = os.path.expanduser("~/models/bge-base-zh-v1.5")

embed_model = HuggingFaceEmbeddings(model_name=model_path)

client = MilvusClient(uri="http://localhost:19530")

MEMORY_COLLECTION = "agent_memory"
KNOWLEDGE_COLLECTION = "demo_collection"


if not client.has_collection(MEMORY_COLLECTION):

    schema = client.create_schema(
        auto_id=False,
        enable_dynamic_field=False,
    )

    schema.add_field(
        field_name="id",
        datatype=DataType.VARCHAR,
        is_primary=True,
        max_length=64,
    )

    schema.add_field(
        field_name="user_id",
        datatype=DataType.VARCHAR,
        max_length=64,
    )

    schema.add_field(
        field_name="memory_key",
        datatype=DataType.VARCHAR,
        max_length=64,
    )

    schema.add_field(
        field_name="memory",
        datatype=DataType.VARCHAR,
        max_length=2000,
    )

    schema.add_field(
        field_name="vector",
        datatype=DataType.FLOAT_VECTOR,
        dim=768,
    )

    index_params = client.prepare_index_params()

    index_params.add_index(
        field_name="vector",
        index_type="AUTOINDEX",
        metric_type="COSINE",
    )

    client.create_collection(
        collection_name=MEMORY_COLLECTION,
        schema=schema,
        index_params=index_params,
    )


#  =========================
#  工具函数
#  =========================
@tool
def calculator(a: int, b: int) -> int:
    """计算两个整数的和"""
    return a + b


@tool
def search_web(query: str) -> str:
    """搜索互联网，获取与查询相关的网页结果。"""

    try:
        response = requests.get(
            "http://localhost:8080/search",
            params={
                "q": query,
                "format": "json",
            },
            timeout=10,
        )

        response.raise_for_status()

        data = response.json()

    except requests.RequestException as e:
        return f"搜索失败：{e!s}"

    results = data.get("results", [])

    if not results:
        return "没有找到相关搜索结果。"

    output = []

    for i, item in enumerate(results[:5], 1):
        output.append(f"""结果 {i}
标题：{item.get('title', '')}
URL：{item.get('url', '')}
摘要：{item.get('content', '')}
""")

    return "\n".join(output)


@tool
def search_knowledge_base(query: str) -> str:
    """在内部知识库中搜索相关信息"""

    if not client.has_collection(KNOWLEDGE_COLLECTION):
        return "内部知识库尚未初始化，无法提供知识库检索结果。"

    # 1. 把用户问题转换成向量
    query_vector = embed_model.embed_query(query)

    # 2. 去 Milvus 做向量搜索
    res = client.search(
        collection_name=KNOWLEDGE_COLLECTION,
        data=[query_vector],
        anns_field="vector",
        limit=5,
        output_fields=["text", "metadata"],
    )

    # 3. 提取搜索结果
    results = []

    for hit in res[0]:
        text = hit["entity"]["text"]
        distance = hit["distance"]

        results.append(f"相似度距离：{distance}\n" f"内容：{text}")

    # 4. 返回给 LLM
    return "\n\n".join(results)


@tool
def fetch_webpage(url: str) -> str:
    """获取网页内容"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=10,
        )

        response.raise_for_status()

        return response.text

    except requests.HTTPError:
        return f"网页访问失败：HTTP {response.status_code}，URL：{url}"

    except requests.RequestException as e:
        return f"网页访问失败：{e!s}"


# print(search_web.invoke("LangChain Agent 是什么"))
# print(type(search_web))
# print(search_web)
# print(search_web.name)
# print(search_web.description)


def _get_memories_by_keys(
    user_id: str,
    memory_keys: list[str],
):
    """读取当前用户指定类别的全部记忆，包括旧版固定主键记录。"""
    if not memory_keys:
        return []

    # Encode literals because the deployed Milvus cannot parse filter templates.
    user_literal = json.dumps(user_id, ensure_ascii=False)
    keys_literal = json.dumps(list(dict.fromkeys(memory_keys)), ensure_ascii=False)
    records = client.query(
        collection_name=MEMORY_COLLECTION,
        filter=f"user_id == {user_literal} and memory_key in {keys_literal}",
        output_fields=[
            "id",
            "user_id",
            "memory_key",
            "memory",
        ],
    )

    return records or []


def _insert_memory(
    user_id: str,
    memory: str,
    memory_key: str,
):
    """插入一条独立的长期记忆，返回新记录 ID。"""
    if not memory or not memory.strip():
        raise ValueError("新增记忆不能为空")

    vector = embed_model.embed_query(memory)
    memory_id = str(uuid.uuid4())
    client.insert(
        collection_name=MEMORY_COLLECTION,
        data=[
            {
                "id": memory_id,
                "vector": vector,
                "user_id": user_id,
                "memory": memory,
                "memory_key": memory_key,
            }
        ],
    )

    return memory_id


@tool
def save_to_memory(user_id: str, memory: str, memory_key: str) -> str:
    """
    Agent 主动把重要结论写入 Milvus，下次会话能检索到
    保存用户的重要长期信息。
    只有当用户提供了未来对话中可能仍然有用的信息时才使用，
    例如用户的长期目标、偏好、项目背景等。
    """

    _insert_memory(
        user_id=user_id,
        memory=memory,
        memory_key=memory_key,
    )

    return f"长期记忆已保存：{memory}"


@tool
def search_memory(user_id: str, query: str) -> str:
    """
    根据用户的问题搜索相关长期记忆。
    搜索用户过去保存的长期记忆。
    """

    query_vector = embed_model.embed_query(query)

    results = client.search(
        collection_name=MEMORY_COLLECTION,
        data=[query_vector],
        limit=5,
        filter=f"user_id == {json.dumps(user_id, ensure_ascii=False)}",
        output_fields=["user_id", "memory"],
    )

    if not results or not results[0]:
        return "没有找到相关长期记忆。"

    output = []

    for hit in results[0]:
        memory = hit["entity"]["memory"]
        distance = hit["distance"]

        output.append(f"相关记忆：{memory}\n" f"距离：{distance}")

    return "\n\n".join(output)


def replace_memory(
    user_id: str,
    memory: str,
    memory_key: str,
    memory_id: str,
) -> str:
    """
    按旧记录 ID 更新记忆；目标必须仍属于当前用户和类别。
    """
    if not memory or not memory.strip():
        raise ValueError("替换记忆不能为空")
    if not memory_id:
        raise ValueError("replace 操作必须提供 memory_id")

    records = client.get(
        collection_name=MEMORY_COLLECTION,
        ids=[memory_id],
        output_fields=["id", "user_id", "memory_key"],
    )
    if len(records or []) != 1:
        raise ValueError("要替换的记忆不存在")
    record = records[0]
    if record["user_id"] != user_id or record["memory_key"] != memory_key:
        raise ValueError("要替换的记忆不属于当前用户或类别")

    vector = embed_model.embed_query(memory)

    client.upsert(
        collection_name=MEMORY_COLLECTION,
        data=[
            {
                "id": memory_id,
                "user_id": user_id,
                "memory": memory,
                "memory_key": memory_key,
                "vector": vector,
            }
        ],
    )

    return memory_id


# =========================
# 5. 检索相关记忆 不知道 key 时做向量搜索兜底
# =========================
def _recall_memories(
    user_id: str,
    query: str,
    limit: int = 5,
    min_score: float = 0.45,
):
    """
    根据当前用户问题召回相关长期记忆。

    流程：
    1. query 转 embedding
    2. Milvus 向量搜索
    3. user_id 过滤
    4. 根据相似度阈值过滤
    5. 返回符合条件的长期记忆
    """

    print("\n========== RECALL DEBUG ==========")
    print("用户:", user_id)
    print("查询:", query)
    print("阈值:", min_score)

    # 1. 用户问题转向量
    query_vector = embed_model.embed_query(query)

    # 2. Milvus 向量搜索
    results = client.search(
        collection_name=MEMORY_COLLECTION,
        data=[query_vector],
        anns_field="vector",
        limit=limit,
        filter=f"user_id == {json.dumps(user_id, ensure_ascii=False)}",
        output_fields=[
            "id",
            "user_id",
            "memory_key",
            "memory",
        ],
    )

    # 3. 先看 Milvus 原始结果
    print("\n---------- RAW RESULTS ----------")
    print(results)

    if not results:
        print("Milvus 没有返回 results")
        return []

    if not results[0]:
        print("Milvus results[0] 为空")
        return []

    # 4. 遍历过滤前的所有结果
    print("\n---------- RAW HITS ----------")

    for hit in results[0]:
        entity = hit["entity"]
        score = hit["distance"]

        print(f"""
        memory_key: {entity.get("memory_key")}
        memory: {entity.get("memory")}
        score: {score}
        """)

    # 5. 根据阈值过滤
    memories = []

    print("\n---------- FILTER ----------")

    for hit in results[0]:
        entity = hit["entity"]
        score = float(hit["distance"])

        if score < min_score:
            print(f"过滤掉：{entity['memory']} " f"score={score:.4f} < {min_score}")
            continue

        print(f"保留：{entity['memory']} " f"score={score:.4f}")

        memories.append(
            {
                "id": entity["id"],
                "user_id": entity["user_id"],
                "memory_key": entity["memory_key"],
                "memory": entity["memory"],
                "distance": score,
            }
        )

    print("\n---------- FINAL RECALL ----------")
    print(memories)

    return memories
