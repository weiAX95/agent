from tools import (
    MEMORY_COLLECTION,
    client,
    embed_model,
)

memory_id = "baa2ff63-e1c2-44e0-a762-2afce6962e15"

memory = "用户主要从事前端开发。"

vector = embed_model.embed_query(memory)

client.upsert(
    collection_name=MEMORY_COLLECTION,
    data=[
        {
            "id": memory_id,
            "user_id": "test_user_v2",
            "memory_key": "career_direction",
            "memory": memory,
            "vector": vector,
        }
    ],
)

print("旧记忆已修改为：", memory)
