"""Load real HTTP handlers without importing model/Milvus startup side effects."""

import ast
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.chat_storage import begin_turn, create_session, owned_session, session_execution_lock
from api.chat_stream import ChatRequest, persisted_chat_response
from api.models import ChatSession

ROOT = Path(__file__).resolve().parents[1]


class MemoryLockPool:
    def __init__(self):
        self.keys = set()
        self.borrowed = 0

    @asynccontextmanager
    async def connection(self):
        pool = self

        class Connection:
            async def execute(self, statement, params):
                key = params[0]
                if "pg_try_advisory_lock" in statement:
                    acquired = key not in pool.keys
                    if acquired:
                        pool.keys.add(key)
                    async def fetchone():
                        return {"acquired": acquired}
                    return SimpleNamespace(fetchone=fetchone)
                pool.keys.discard(key)

            async def close(self):
                pass

        self.borrowed += 1
        try:
            yield Connection()
        finally:
            self.borrowed -= 1


def load_definitions(path, names, namespace):
    tree = ast.parse(path.read_text())
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            if not isinstance(node, ast.ClassDef):
                node.decorator_list = []
            nodes.append(node)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), namespace)


def make_app(factory, pool, model):
    async def get_db():
        async with factory() as db:
            yield db

    async def get_user(x_test_user: str | None = Header(default=None)):
        if x_test_user is None:
            raise HTTPException(401, "test authentication required")
        return {"user_id": x_test_user}

    ns = {
        "uuid": uuid, "HTTPException": HTTPException, "Request": Request,
        "ChatRequest": ChatRequest, "AsyncSession": AsyncSession, "Depends": Depends,
        "get_current_user": get_user, "get_db": get_db,
        "owned_session": owned_session, "begin_turn": begin_turn,
        "session_execution_lock": session_execution_lock,
        "persisted_chat_response": persisted_chat_response,
        "chat_stream": model, "_usage_payload": lambda _: {"available": False},
        "logger": logging.getLogger("chat-api-fixture"),
    }
    load_definitions(ROOT / "api/server.py", {"get_chat_turn", "chat_api"}, ns)
    ns.update(BaseModel=BaseModel, ConfigDict=ConfigDict, Field=Field,
              ChatSession=ChatSession, create_session=create_session)
    load_definitions(ROOT / "api/routers/sessions.py", {"SessionCreate", "owner_id", "session_data", "create_chat_session"}, ns)
    app = FastAPI()
    app.state.agent = object()
    app.state.chat_lock_pool = pool
    app.post("/api/sessions", status_code=201)(ns["create_chat_session"])
    # Use the production decorator path so a missing route migration fails tests.
    server_tree = ast.parse((ROOT / "api/server.py").read_text())
    handler = next(node for node in server_tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat_api")
    route_path = handler.decorator_list[0].args[0].value
    app.post(route_path)(ns["chat_api"])
    return app
