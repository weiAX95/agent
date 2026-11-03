"""Shared document vectors with PostgreSQL as the active-revision authority."""

import json
import os
import uuid

import psycopg
from pymilvus import DataType

from src.agent.memory.client import get_client, get_embed_model

KNOWLEDGE_COLLECTION = "agent_knowledge_v1"
MIN_KNOWLEDGE_SCORE = 0.40  # Initial COSINE cutoff; tune against a fixed question set.


def _score_threshold(value: float | None) -> float:
    raw = os.getenv("KNOWLEDGE_MIN_SCORE", str(MIN_KNOWLEDGE_SCORE)) if value is None else value
    try:
        score = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("KNOWLEDGE_MIN_SCORE 必须是 0 到 1 之间的数字") from exc
    if not 0 <= score <= 1:
        raise ValueError("KNOWLEDGE_MIN_SCORE 必须是 0 到 1 之间的数字")
    return score


def ensure_knowledge_collection() -> None:
    """Create only the new managed collection; never touch demo_collection."""
    client = get_client()
    if client.has_collection(KNOWLEDGE_COLLECTION):
        client.load_collection(KNOWLEDGE_COLLECTION)
        return
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(field_name="id", datatype=DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field(field_name="document_id", datatype=DataType.VARCHAR, max_length=64)
    schema.add_field(field_name="revision", datatype=DataType.VARCHAR, max_length=64)
    schema.add_field(field_name="chunk_index", datatype=DataType.INT64)
    schema.add_field(field_name="filename", datatype=DataType.VARCHAR, max_length=255)
    schema.add_field(field_name="page", datatype=DataType.INT64)
    schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=4096)
    schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=768)
    indexes = client.prepare_index_params()
    indexes.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
    try:
        client.create_collection(
            collection_name=KNOWLEDGE_COLLECTION, schema=schema, index_params=indexes,
        )
    except Exception:
        # Another API worker may have created the collection after our check.
        if not client.has_collection(KNOWLEDGE_COLLECTION):
            raise
    client.load_collection(KNOWLEDGE_COLLECTION)


def _milvus_string(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def index_document(document_id: uuid.UUID, revision: uuid.UUID, filename: str, chunks) -> int:
    """Insert a new immutable revision; caller switches the DB revision after success."""
    ensure_knowledge_collection()
    client = get_client()
    embedding = get_embed_model()
    for start in range(0, len(chunks), 32):
        batch = chunks[start:start + 32]
        vectors = embedding.embed_documents([chunk.text for chunk in batch])
        if len(vectors) != len(batch):
            raise RuntimeError("Embedding 数量与文档分块不一致")
        entities = [
            {
                "id": str(uuid.uuid4()),
                "document_id": str(document_id),
                "revision": str(revision),
                "chunk_index": start + offset,
                "filename": _milvus_string(filename, 255),
                "page": chunk.page,
                "text": chunk.text,
                "vector": vector,
            }
            for offset, (chunk, vector) in enumerate(zip(batch, vectors))
        ]
        client.insert(collection_name=KNOWLEDGE_COLLECTION, data=entities)
    return len(chunks)


def delete_document_chunks(document_id: uuid.UUID, revision: uuid.UUID | None = None) -> None:
    client = get_client()
    if not client.has_collection(KNOWLEDGE_COLLECTION):
        return
    expression = f"document_id == {json.dumps(str(document_id))}"
    if revision is not None:
        expression += f" && revision == {json.dumps(str(revision))}"
    client.delete(collection_name=KNOWLEDGE_COLLECTION, filter=expression)


def _active_revisions(document_ids: set[str]) -> dict[str, tuple[str, str]]:
    """A candidate is usable only after its metadata transaction has committed."""
    if not document_ids:
        return {}
    from api.config import settings
    from src.agent.checkpoint import checkpoint_dsn

    ids = [uuid.UUID(value) for value in document_ids]
    placeholders = ", ".join("%s" for _ in ids)
    query = (
        "SELECT id::text, revision::text, filename FROM knowledge_documents "
        f"WHERE deleted_at IS NULL AND id IN ({placeholders})"
    )
    with psycopg.connect(checkpoint_dsn(settings.database_url), connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, ids)
            return {doc_id: (revision, filename) for doc_id, revision, filename in cursor.fetchall()}


def search_knowledge_chunks(
    query: str, limit: int = 5, min_score: float | None = None,
) -> list[dict]:
    """Return only live chunks from the shared catalog, in COSINE score order."""
    if not query.strip():
        return []
    if not 1 <= limit <= 20:
        raise ValueError("知识库检索数量无效")
    threshold = _score_threshold(min_score)
    client = get_client()
    if not client.has_collection(KNOWLEDGE_COLLECTION):
        return []
    vector = get_embed_model().embed_query(query[:2000])
    search_limit = max(20, limit * 5)
    while True:
        results = client.search(
            collection_name=KNOWLEDGE_COLLECTION,
            data=[vector], anns_field="vector",
            search_params={"metric_type": "COSINE"},
            limit=search_limit,
            output_fields=["document_id", "revision", "chunk_index", "filename", "page", "text"],
            consistency_level="Strong",
        )
        hits = results[0] if results else []
        candidates = [hit for hit in hits if float(hit.get("distance", -1)) >= threshold]
        active = _active_revisions({hit["entity"]["document_id"] for hit in candidates})
        found = []
        for hit in candidates:
            entity = hit["entity"]
            doc_id = entity["document_id"]
            current = active.get(doc_id)
            if current is None or current[0] != entity["revision"]:
                continue
            if not current[1].strip() or not isinstance(entity["text"], str) or not entity["text"].strip():
                continue
            page = int(entity["page"])
            found.append({
                "document_id": doc_id,
                "chunk_id": str(hit["id"]),
                "title": current[1],
                "page": page if page > 0 else None,
                "text": entity["text"],
                "score": round(float(hit["distance"]), 4),
            })
            if len(found) >= limit:
                return found
        # Old revisions and incomplete writes are filtered by PostgreSQL.
        # Widen the candidate window only while a better live hit may remain.
        if len(hits) < search_limit or len(candidates) < len(hits) or search_limit >= 1000:
            return found
        search_limit = min(1000, search_limit * 5)
