"""Compatibility imports for existing tool-module callers."""
from src.agent.memory.client import KNOWLEDGE_COLLECTION, MEMORY_COLLECTION, client, embed_model
from src.agent.memory.store import _get_memories_by_keys, _insert_memory, _recall_memories, replace_memory, save_to_memory, search_memory
from src.agent.web_tools import calculator, fetch_webpage, search_knowledge_base, search_web
