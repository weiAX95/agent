"""Lazy Milvus and embedding resources for the production Agent."""

import os
from functools import lru_cache

from langchain_huggingface import HuggingFaceEmbeddings
from pymilvus import DataType, MilvusClient

MEMORY_COLLECTION = "agent_memory"
KNOWLEDGE_COLLECTION = "demo_collection"


@lru_cache(maxsize=1)
def get_embed_model():
    model_path = os.path.expanduser("~/models/bge-base-zh-v1.5")
    return HuggingFaceEmbeddings(model_name=model_path)


def _ensure_memory_collection(connection):
    if connection.has_collection(MEMORY_COLLECTION):
        return
    schema = connection.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(field_name="id", datatype=DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field(field_name="user_id", datatype=DataType.VARCHAR, max_length=64)
    schema.add_field(field_name="memory_key", datatype=DataType.VARCHAR, max_length=64)
    schema.add_field(field_name="memory", datatype=DataType.VARCHAR, max_length=2000)
    schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=768)
    index_params = connection.prepare_index_params()
    index_params.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
    connection.create_collection(
        collection_name=MEMORY_COLLECTION, schema=schema, index_params=index_params
    )


@lru_cache(maxsize=1)
def get_client():
    connection = MilvusClient(uri=os.getenv("MILVUS_URI", "http://localhost:19530"))
    try:
        _ensure_memory_collection(connection)
    except Exception:
        connection.close()
        raise
    return connection


def initialize_memory_resources():
    """Fail startup early if embeddings or Milvus cannot be initialized."""
    get_embed_model()
    get_client()


def close_memory_resources():
    if get_client.cache_info().currsize:
        get_client().close()
        get_client.cache_clear()
    get_embed_model.cache_clear()


class _DeferredResource:
    """Keep existing tool imports usable without importing external resources."""

    def __init__(self, factory):
        self._factory = factory

    def __getattr__(self, name):
        return getattr(self._factory(), name)


client = _DeferredResource(get_client)
embed_model = _DeferredResource(get_embed_model)
