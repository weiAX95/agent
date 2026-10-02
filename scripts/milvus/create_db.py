from pathlib import Path
from pprint import pprint

from pymilvus import DataType, MilvusClient

# 项目根目录（scripts/milvus/create_db.py → scripts/milvus → scripts → 根目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 实例化向量数据库客户端（使用本地 Milvus Lite 模式）

client = MilvusClient(uri="http://localhost:19530")

# 创建 Schema
def build_schema():
    """构建集合的 Schema 结构"""
    # 1. 初始化 Schema 对象
    schema = MilvusClient.create_schema(
        auto_id=True,  # 自动分配主键
        enable_dynamic_field=True,  # 启用动态字段，支持未声明字段的键值对存储
    )

    # 2. 分步添加字段（避免链式调用返回 None 导致的报错）
    schema.add_field(
        field_name="id", datatype=DataType.INT64, is_primary=True
    )
    schema.add_field(
        field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=768
    )
    schema.add_field(
        field_name="text", datatype=DataType.VARCHAR, max_length=1024
    )
    schema.add_field(field_name="metadata", datatype=DataType.JSON)

    return schema


# 创建索引参数
def build_index():
    """构建向量索引参数"""
    index_params = MilvusClient.prepare_index_params()
    index_params.add_index(
        field_name="vector",  # 建立索引的字段
        index_type="AUTOINDEX",  # 索引类型（自动选择最优算法）
        metric_type="L2",  # 向量相似度度量方式（欧氏距离）
    )
    return index_params


# 创建 Collection
COLLECTION_NAME = "demo_collection"

if client.has_collection(collection_name=COLLECTION_NAME):
    # 删除已存在的 Collection
    # 注：Milvus 删除数据后空间不会立即释放，依赖后台 GC 进程定期清理和合并数据段
    client.drop_collection(collection_name=COLLECTION_NAME)

# 一步到位：创建集合并自动建立索引、加载至内存
client.create_collection(
    collection_name=COLLECTION_NAME,
    schema=build_schema(),
    index_params=build_index(),
)

# 查看当前所有 Collection
print(client.list_collections())

# 查看 Collection 的详细描述信息
pprint(client.describe_collection(collection_name=COLLECTION_NAME))