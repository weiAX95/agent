from pymilvus import MilvusClient

client = MilvusClient(uri="http://localhost:19530")

res = client.delete(
    collection_name="demo_collection",
    filter="",
    ids=[461484610130804912, 461484610130804913]
)

print("删除结果：", res)

res2 = client.delete(
    collection_name="demo_collection",
    filter='text LIKE "第%"',
)

print("删除结果：", res2)