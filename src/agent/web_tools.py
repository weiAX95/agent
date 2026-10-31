"""External research tools used by the research subagent."""
import requests
from langchain_core.tools import tool
from src.agent.memory.client import KNOWLEDGE_COLLECTION, client, embed_model

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
