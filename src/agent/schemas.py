from typing import Any, TypedDict

from langchain_core.messages import BaseMessage
from pydantic import BaseModel


class AgentState(TypedDict):

    messages: list[BaseMessage]

    user_id: str

    session_id: str

    memory_candidate: dict[str, Any] | None

    related_memories: list[dict[str, Any]] | None

    memory_resolution: dict[str, Any] | None


class MemoryDecision(BaseModel):

    should_save: bool
    memory_key: str | None = None

    memory: str | None = None


class MemoryResolution(BaseModel):

    action: str

    memory_id: str | None = None

    reason: str
