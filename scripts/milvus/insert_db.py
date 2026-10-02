import os
from pathlib import Path

from langchain_community.document_loaders import UnstructuredWordDocumentLoader
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pymilvus import MilvusClient

# 项目根目录（scripts/milvus/insert_db.py → scripts/milvus → scripts → 根目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 1. 实例化向量数据库客户端（连接 Milvus Standalone 服务）
client = MilvusClient(uri="http://localhost:19530")

# 2. 加载文档
# 注意：确保 assets/sample.docx 文件存在，否则会抛出 FileNotFoundError
loader = UnstructuredWordDocumentLoader(file_path=str(PROJECT_ROOT / "assets" / "sample.docx"), mode="single")
docs = loader.load()

# 3. 文档切分
# separators 末尾的 "" 是兜底策略，当其他分隔符都无法切分时，会强制按字符切分
text_splitter = RecursiveCharacterTextSplitter(
    separators=["\n\n", "\n", "。", "！", "？", "……", "，", ""],
    chunk_size=400,
    chunk_overlap=50,
)
chunks = text_splitter.split_documents(docs)

# 4. 加载嵌入模型
# 使用 os.path.expanduser 处理 ~ 路径，确保跨平台兼容性
model_path = os.path.expanduser("~/models/bge-base-zh-v1.5")
embed_model = HuggingFaceEmbeddings(model_name=model_path)

# 5. 计算嵌入向量
# 提取每个 chunk 的文本内容进行向量化
texts = [chunk.page_content for chunk in chunks]
embeddings = embed_model.embed_documents(texts)

# 6. 转换数据格式并插入
# 确保 vector 维度与 Schema 定义一致（如 768），且 metadata 可被 JSON 序列化
data = [
    {
        "vector": embedding,
        "text": chunk.page_content,
        "metadata": chunk.metadata,
    }
    for chunk, embedding in zip(chunks, embeddings)
]

# 执行插入操作
res = client.insert(
    collection_name="demo_collection",
    data=data
)

print(f"成功插入 {res['insert_count']} 条数据")

# 立即查询
client.load_collection("demo_collection")

check = client.query(
    collection_name="demo_collection",
    filter="",
    output_fields=["*"],
    limit=5,
)

print("插入后立即查询：")
print(check)

count = client.query(
    collection_name="demo_collection",
    filter="",
    output_fields=["count(*)"],
)

print("插入后数量：")
print(count)