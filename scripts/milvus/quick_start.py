import os

from langchain_huggingface import HuggingFaceEmbeddings
from pymilvus import MilvusClient

# 加载嵌入模型（和你 RAG 部分保持一致）
embed_model = HuggingFaceEmbeddings(
    model_name=os.path.expanduser("~/models/bge-base-zh-v1.5")
)

client = MilvusClient(uri="http://localhost:19530")

print(client.list_collections())

# 如果 collection 已存在，先删除重建（demo 场景）
if client.has_collection(collection_name="demo_collection"):
    client.drop_collection(collection_name="demo_collection")

docs = [
    "人工智能是研究、开发用于模拟、延伸和扩展人的智能的理论、方法、技术及应用系统的一门新的技术科学。",
    "机器学习是人工智能的一个分支，它是一种通过数据训练模型的方法。",
    "深度学习是机器学习的一个子集，使用神经网络进行学习。",
]

# 用 bge 模型生成向量，而不是 pymilvus 默认的 onnx 模型
vectors = embed_model.embed_documents(docs)

client.create_collection(
    collection_name="demo_collection",
    dimension=len(vectors[0]),  # bge-base-zh-v1.5 是 768 维
)

data = [
    {"id": i, "vector": vectors[i], "text": docs[i], "subject": "history"}
    for i in range(len(docs))
]

res = client.insert(collection_name="demo_collection", data=data)
print(res)

# 测试检索
query_vectors = embed_model.embed_query("什么是人工智能？")

res = client.search(
    collection_name="demo_collection",
    data=[query_vectors],
    limit=2,
    output_fields=["text", "subject"],
)
print(res)