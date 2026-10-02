from graph import graph

result = graph.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": "我现在决定转向全栈开发，以后全栈开发才是我的主要职业方向。",
            }
        ],
        "user_id": "test_user_v2",
        "session_id": "test_session",
        "memory_candidate": None,
        "related_memories": None,
        "memory_resolution": None,
    }
)


print("\n========== 最终消息 ==========")

for message in result["messages"]:
    print(type(message).__name__)
    print(message.content)
    print()
print("========== Memory Candidate ==========")

print(result["memory_candidate"])


print("\n========== Related Memories ==========")

print(result["related_memories"])


print("\n========== Memory Resolution ==========")

print(result["memory_resolution"])


print("\n========== Graph Mermaid ==========")

print(graph.get_graph().draw_mermaid())
