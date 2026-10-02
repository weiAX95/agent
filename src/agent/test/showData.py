from agent.src.agent.tools import MEMORY_COLLECTION, client

res = client.query(
    collection_name=MEMORY_COLLECTION,
    filter='user_id == "test_user_v2"',
    output_fields=[
        "id",
        "user_id",
        "memory_key",
        "memory",
    ],
)

for item in res:
    print(item)
