import os

import dotenv
from langchain_openai import ChatOpenAI

dotenv.load_dotenv()


model = ChatOpenAI(
    model=os.getenv("OPENAI_MODEL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_API_BASE_URL"),
)
