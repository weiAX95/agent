import time

from smith import agent

start = time.time()

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": "我现在决定以后往全栈开发方向发展，Agent 只是我的 AI 能力补充。",
            }
        ]
    }
)


print("\n========== 执行过程 ==========")

for i, message in enumerate(result["messages"]):

    print(f"\n--- Message {i} ---")

    print("类型：", type(message).__name__)

    print("内容：", message.content)

    if hasattr(message, "tool_calls"):
        print("Tool Calls：", message.tool_calls)


print("\n========== 总耗时 ==========")

print(f"{time.time() - start:.2f}s")
