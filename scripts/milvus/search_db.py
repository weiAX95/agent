import os

from langchain_huggingface import HuggingFaceEmbeddings
from pymilvus import MilvusClient

# 1. 连接 Milvus Standalone 服务
client = MilvusClient(uri="http://localhost:19530")


# 2. 加载和建库时完全相同的 Embedding 模型
model_path = os.path.expanduser("~/models/bge-base-zh-v1.5")

embed_model = HuggingFaceEmbeddings(
    model_name=model_path
)


# 3. 用户的问题
query = "LangChain 为什么需要使用？"


# 4. 把用户的问题转换成向量
query_vector = embed_model.embed_query(query)

print("问题：", query)
print("问题向量维度：", len(query_vector))


# 5. 到 Milvus 中进行向量相似度搜索
res = client.search(
    collection_name="demo_collection",
    data=[query_vector],
    anns_field="vector",
    limit=5,
    output_fields=["text", "metadata"],
)


# 6. 打印检索结果
print("\n========== 检索结果 ==========\n")

for i, hit in enumerate(res[0], 1):
    print(f"第 {i} 条")
    print("距离：", hit["distance"])
    print("文本：", hit["entity"]["text"])
    print("来源：", hit["entity"]["metadata"])
    print("-" * 80)