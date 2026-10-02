"""
Gemini API 测试脚本

优先从 .env 文件加载 GEMINI_API_KEY 和 GEMINI_MODEL。

依赖：
    pip install python-dotenv requests certifi

.env 示例：
    GEMINI_API_KEY=你的API_KEY
    GEMINI_MODEL=gemini-3.6-flash

运行：
    python test_gemini.py
"""

import json
import os
import ssl
import urllib.error
import urllib.request

import dotenv

# 加载 .env 文件
dotenv.load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

if not API_KEY:
    print("❌ 请先设置 GEMINI_API_KEY（.env 文件或环境变量）")
    raise SystemExit(1)


def _get_ssl_context():
    """创建使用 TLS 1.2+ 的 SSL 上下文。"""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def http_get(url: str) -> dict:
    """优先使用 requests，回退到 urllib。"""
    try:
        import requests
        requests.get(
    "https://generativelanguage.googleapis.com/v1beta/models",
    params={
        "key": API_KEY,
        "pageSize": 50
    },
)
        response.raise_for_status()
        return response.json()
    except ImportError:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=30, context=_get_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))


def http_post(url: str, payload: dict) -> dict:
    """优先使用 requests，回退到 urllib。"""
    try:
        import requests
        requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
            params={"key": API_KEY},
            json=payload,
        )
        response.raise_for_status()
        return response.json()
    except ImportError:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=60, context=_get_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))


def list_models():
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={API_KEY}&pageSize=50"
    print("正在列出可用模型...")
    try:
        data = http_get(url)
        models = data.get("models", [])
        print(f"找到 {len(models)} 个模型")
        for m in models:
            name = m.get("name", "")
            methods = ", ".join(m.get("supportedGenerationMethods", [])[:3])
            if "generateContent" in m.get("supportedGenerationMethods", []):
                print(f"  - {name} ({methods})")
    except Exception as e:
        print(f"列出模型失败: {e}")


def chat(message: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={API_KEY}"
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": message}]}
        ]
    }
    result = http_post(url, payload)
    return result["candidates"][0]["content"]["parts"][0]["text"]


if __name__ == "__main__":
    print(f"当前模型: {MODEL}\n")

    # 列出支持 generateContent 的模型
    list_models()

    # 简单对话测试
    print(f"\n发送测试消息...")
    try:
        reply = chat("你好，请用一句话介绍自己")
        print(f"模型回复: {reply}")
    except urllib.error.URLError as e:
        print(f"\n网络请求失败: {e}")
        print("建议排查：")
        print("1. 安装 requests + certifi: pip install requests certifi")
        print("2. 检查是否需要代理/VPN 访问 Google API")
        print("3. 升级 Python 到 3.11+")
    except Exception as e:
        print(f"请求失败: {e}")
