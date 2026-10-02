import os

from pymilvus import DataType, MilvusClient

COLLECTION_NAME = "agent_memory"
MILVUS_URI = os.getenv(
    "MILVUS_URI",
    "http://localhost:19530",
)


def init_collection():
    client = MilvusClient(uri=MILVUS_URI)

    # 如果已经存在，开发阶段直接删除旧 collection
    if client.has_collection(COLLECTION_NAME):
        print(f"删除旧 collection: {COLLECTION_NAME}")
        client.drop_collection(COLLECTION_NAME)

    # =========================
    # 1. 创建 Schema
    # =========================
    schema = client.create_schema(
        auto_id=False,
        enable_dynamic_field=False,
    )

    # 主键
    schema.add_field(
        field_name="id",
        datatype=DataType.VARCHAR,
        is_primary=True,
        max_length=64,
    )

    # 用户 ID
    schema.add_field(
        field_name="user_id",
        datatype=DataType.VARCHAR,
        max_length=64,
    )

    # 记忆类型 / 槽位
    schema.add_field(
        field_name="memory_key",
        datatype=DataType.VARCHAR,
        max_length=64,
    )

    # 具体记忆内容
    schema.add_field(
        field_name="memory",
        datatype=DataType.VARCHAR,
        max_length=2000,
    )

    # 向量
    schema.add_field(
        field_name="vector",
        datatype=DataType.FLOAT_VECTOR,
        dim=768,
    )

    # =========================
    # 2. 创建 Collection
    # =========================
    client.create_collection(
        collection_name=COLLECTION_NAME,
        schema=schema,
    )

    # =========================
    # 3. 创建向量索引
    # =========================
    index_params = client.prepare_index_params()

    index_params.add_index(
        field_name="vector",
        index_type="AUTOINDEX",
        metric_type="COSINE",
    )

    client.create_index(
        collection_name=COLLECTION_NAME,
        index_params=index_params,
    )
    client.load_collection(collection_name=COLLECTION_NAME)
    print()
    print("================================")
    print("agent_memory 创建成功")
    print("================================")
    print("Milvus:", MILVUS_URI)
    print("Collection:", COLLECTION_NAME)
    print()
    print("Schema:")
    print("  id         VARCHAR")
    print("  user_id    VARCHAR")
    print("  memory_key VARCHAR")
    print("  memory     VARCHAR")
    print("  vector     FLOAT_VECTOR(768)")
    print()


if __name__ == "__main__":
    init_collection()
