import os
import requests
from dotenv import load_dotenv

load_dotenv()

# 阿里百炼 OpenAI 兼容接口
api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("api-key")
base_url = "https://llm-ooz654jabq5ccnfn.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
url = f"{base_url}/chat/completions"

if not api_key:
    print("❌ 缺少 API Key，请在 .env 中设置 DASHSCOPE_API_KEY 或 OPENAI_API_KEY")
    raise SystemExit(1)

payload = {
    "model": "qwen-plus",  # 百炼支持的模型，可换成 qwen-max、qwen-turbo 等
    "messages": [
        {"role": "system", "content": "你是一个 helpful assistant"},
        {"role": "user", "content": "你好，请用一句话介绍自己"}
    ],
    "max_tokens": 200,
    "temperature": 0.7,
}

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
}

print(f"请求 URL: {url}")
print(f"模型: {payload['model']}")

response = requests.post(url, json=payload, headers=headers, timeout=60)

print("HTTP Status:", response.status_code)
print(response.text)

if response.status_code == 200:
    result = response.json()
    content = result["choices"][0]["message"]["content"]
    print(f"\n模型回复: {content}")
