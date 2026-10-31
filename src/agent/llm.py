"""Create the production chat model only when the API starts."""

import os
from functools import lru_cache

from langchain.chat_models import init_chat_model


@lru_cache(maxsize=1)
def get_llm():
    return init_chat_model(
        model=os.getenv("OPENAI_MODEL"),
        model_provider="openai",
        base_url=os.getenv("OPENAI_BASE_URL"),
        api_key=os.getenv("OPENAI_API_KEY"),
    )
