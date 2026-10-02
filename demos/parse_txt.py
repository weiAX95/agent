from pathlib import Path

from langchain_community.document_loaders import TextLoader

# 项目根目录（demos/parse_txt.py → demos → 根目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

docs = TextLoader(
    file_path=str(PROJECT_ROOT / "assets" / "sample.txt"),  # 文件路径
    encoding="utf-8",  # 文件编码方式
).load()
# 返回 List[Document]

print(docs)
# [Document(metadata={'source': 'assets/sample.txt'}, page_content='...')]