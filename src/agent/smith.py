from langchain.agents import create_agent
from model import model
from tools import (
    fetch_webpage,
    search_knowledge_base,
    search_memory,
    search_web,
)

agent = create_agent(
    model=model,
    tools=[
        search_web,
        fetch_webpage,
        search_knowledge_base,
        search_memory,
    ],
    system_prompt="""
        你是一个有工具能力的 AI 助手。

        当用户的问题需要互联网信息时，可以使用 search_web。
        当需要获取网页详细内容时，可以使用 fetch_webpage。
        当问题涉及内部知识库时，可以使用 search_knowledge_base。
        当用户询问自己的历史信息、长期目标、偏好或过去项目背景时，可以使用 search_memory。

        不要主动保存长期记忆。
        长期记忆由系统单独处理。
""",
)
