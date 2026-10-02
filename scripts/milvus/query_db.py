from pymilvus import MilvusClient

client = MilvusClient(uri="http://localhost:19530")

print("=== 1. Collections ===")
print(client.list_collections())

print("\n=== 2. Collection Info ===")
print(client.describe_collection("demo_collection"))

print("\n=== 3. Loading ===")
client.load_collection("demo_collection")
print("loaded!")

print("\n=== 4. Query ===")
res = client.query(
    collection_name="demo_collection",
    filter="",
    output_fields=["*"],
    limit=5,
)

print(res)

print("\n=== 5. Query Count ===")
res = client.query(
    collection_name="demo_collection",
    filter="",
    output_fields=["id", "text", "metadata"],
    limit=1000
)

print("查询到:", len(res), "条")

for item in res:
    print(item["id"], item["text"][:100])
# count = client.query(
#     collection_name="demo_collection",
#     filter="",
#     output_fields=["count(*)"],
# )
