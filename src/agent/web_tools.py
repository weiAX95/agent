"""External research tools used by the research subagent."""
import ipaddress
import json
import logging
import socket
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool
from src.agent.knowledge_store import search_knowledge_chunks

logger = logging.getLogger(__name__)

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

    results = data.get("results", []) if isinstance(data, dict) else []

    if not isinstance(results, list) or not results:
        return "没有找到相关搜索结果。"

    output = []

    for i, item in enumerate(results[:5], 1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", ""))[:200]
        url = str(item.get("url", ""))[:2048]
        excerpt = str(item.get("content", ""))[:500]
        output.append(f"""结果 {i}
标题：{title}
URL：{url}
摘要：{excerpt}
""")

    return "\n".join(output) if output else "没有找到相关搜索结果。"


@tool
def search_knowledge_base(query: str) -> str:
    """检索共享知识库，返回可核对的文档片段与来源。"""
    try:
        hits = search_knowledge_chunks(query, limit=5)
    except Exception:
        logger.exception("Knowledge retrieval failed")
        return json.dumps({"status": "unavailable", "hits": []}, ensure_ascii=False)
    return json.dumps(
        {"status": "ok" if hits else "no_results", "hits": hits},
        ensure_ascii=False,
    )


@tool
def fetch_webpage(url: str) -> str:
    """读取公开网页的有限正文，不访问本机和内网地址。"""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
            return "网页访问失败：只允许公开的 HTTP/HTTPS 网页。"
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            return "网页访问失败：不允许访问本机或内网地址。"
    except (OSError, ValueError):
        return "网页访问失败：网址无效或无法解析。"

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
        # A model-selected URL must not inherit local proxies or netrc secrets.
        with requests.Session() as session:
            session.trust_env = False
            with session.get(
                url, headers=headers, timeout=(3, 10), stream=True,
                allow_redirects=False,
            ) as response:
                if 300 <= response.status_code < 400:
                    return "网页访问失败：目标网页发生跳转。"
                response.raise_for_status()
                media_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if media_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                    return "网页访问失败：只支持网页或纯文本内容。"
                raw = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    raw.extend(chunk)
                    if len(raw) > 1_000_000:
                        return "网页访问失败：网页正文过大。"
                try:
                    body = raw.decode(response.encoding or "utf-8", errors="replace")
                except LookupError:
                    body = raw.decode("utf-8", errors="replace")
        if media_type == "text/plain":
            return body[:20_000]
        page = BeautifulSoup(body, "html.parser")
        for element in page(["script", "style", "noscript", "nav", "footer"]):
            element.decompose()
        return page.get_text(" ", strip=True)[:20_000]
    except requests.RequestException:
        logger.exception("Public webpage retrieval failed")
        return "网页访问失败：网络或服务器错误。"


# print(search_web.invoke("LangChain Agent 是什么"))
# print(type(search_web))
# print(search_web)
# print(search_web.name)
# print(search_web.description)
