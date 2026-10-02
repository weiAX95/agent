import os
import sys
from pathlib import Path

import dotenv
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

# from langgraph.checkpoint.memory import MemorySaver       # 内存存储（开发调试用）
# from langgraph.checkpoint.sqlite import SqliteSaver        # SQLite 持久化（生产用）
# from langgraph.graph import StateGraph, START, END, MessagesState
# from langgraph.prebuilt import ToolNode, tools_condition
# 将项目根目录加入 sys.path，以便跨模块导入

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent.tools import fetch_webpage, search_web

dotenv.load_dotenv()

llm = ChatOpenAI(
    model=os.getenv("OPENAI_MODEL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_API_BASE_URL"),
)   
# app = graph.compile(checkpointer=MemorySaver())
# config = {"configurable": {"thread_id": "user-session-001"}}
tools = [
    search_web,
    fetch_webpage,
]

tool_map = {
    tool.name: tool
    for tool in tools
}

llm_with_tools = llm.bind_tools([search_web , fetch_webpage])

sessions = {}
def chat(message: str, session_id: str):
    history = sessions.get(session_id, [])
    history.append(
        HumanMessage(content=message)
    )
# 5. Agent Loop
    MAX_ITERATIONS = 5
    for iteration in range(MAX_ITERATIONS):

        print(f"\n========== 第 {iteration + 1} 轮 ==========")


        # =========================
        # 调用 LLM
        # =========================

        res = llm_with_tools.invoke(history)

        history.append(res)


        print("LLM：")
        print(res.content)

        print("Tool Calls：")
        print(res.tool_calls)


        # =========================
        # 没有 Tool Call
        #    Agent 结束
        # =========================

        if not res.tool_calls:

            print("\nAgent 完成任务")

            break


        # =========================
        # 7. 执行 Tool
        # =========================

        for tool_call in res.tool_calls:

            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool_call_id = tool_call["id"]


            print("\n准备调用工具：")
            print("工具：", tool_name)
            print("参数：", tool_args)


            # =========================
            # 8. 查找工具
            # =========================

            tool = tool_map.get(tool_name)


            if tool is None:

                tool_result = (
                    f"工具 {tool_name} 不存在。"
                )

            else:

                # =========================
                # 9. Tool 异常处理
                # =========================

                try:

                    tool_result = tool.invoke(
                        tool_args
                    )

                except Exception as e:

                    tool_result = (
                        f"工具执行失败："
                        f"{type(e).__name__}: {e}"
                    )

                    print("\n工具执行异常：")
                    print(tool_result)


            # =========================
            # 10. 把 Tool Result
            #     返回给 LLM
            # =========================

            history.append(
                ToolMessage(
                    content=str(tool_result),
                    tool_call_id=tool_call_id,
                )
            )


    else:

        # =========================
        # 11. 达到最大轮次
        # =========================

        print(
            f"\nAgent 达到最大轮次限制："
            f"{MAX_ITERATIONS}"
        )
    # 无论 Agent 正常结束还是达到最大轮次，
    # 都把本次对话历史保存到 session
    sessions[session_id] = history


    return history[-1].content

