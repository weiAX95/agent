"""Feedback must be tied to an owned chat log and resist duplicate submission."""

import ast
import asyncio
import copy
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import column, select, table
from sqlalchemy.exc import IntegrityError


ROOT = Path(__file__).resolve().parents[1]


def load_endpoint(namespace):
    path = ROOT / "api/routers/feedback.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    node = copy.deepcopy(next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == "submit_feedback"))
    node.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["submit_feedback"]


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class FakeDB:
    def __init__(self, owned=True, duplicate=False):
        self.owned = owned
        self.duplicate = duplicate
        self.statement = None
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(scalar_one_or_none=lambda: object() if self.owned else None)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        if self.duplicate:
            raise IntegrityError("INSERT", {}, Exception("duplicate"))
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FeedbackApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log_table = table("chat_logs", column("id"), column("user_id"))
        self.endpoint = load_endpoint({
            "select": lambda _: select(self.log_table),
            "Depends": lambda dependency: None,
            "get_current_user": lambda: None,
            "get_db": lambda: None,
            "ChatLog": SimpleNamespace(id=self.log_table.c.id, user_id=self.log_table.c.user_id),
            "ChatFeedback": lambda **fields: SimpleNamespace(**fields),
            "HTTPException": FakeHTTPException,
            "IntegrityError": IntegrityError,
            "uuid": uuid,
        })
        self.chat_log_id = uuid.uuid4()
        self.payload = SimpleNamespace(chat_log_id=self.chat_log_id, category="answer")

    async def test_records_owned_chat_log_without_copying_message(self):
        db = FakeDB()
        response = await self.endpoint(self.payload, {"user_id": "alice"}, db)
        self.assertEqual(response["status"], "recorded")
        self.assertEqual(response["feedback_id"], str(db.added[0].id))
        self.assertEqual((db.added[0].chat_log_id, db.added[0].user_id, db.added[0].category), (self.chat_log_id, "alice", "answer"))
        self.assertNotIn("message", vars(db.added[0]))
        self.assertEqual(db.commits, 1)
        sql = str(db.statement.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("chat_logs.user_id = 'alice'", sql)
        self.assertIn(self.chat_log_id.hex, sql)

    async def test_other_users_chat_log_returns_404(self):
        db = FakeDB(owned=False)
        with self.assertRaises(FakeHTTPException) as error:
            await self.endpoint(self.payload, {"user_id": "mallory"}, db)
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(db.added, [])

    async def test_duplicate_feedback_returns_409(self):
        db = FakeDB(duplicate=True)
        with self.assertRaises(FakeHTTPException) as error:
            await self.endpoint(self.payload, {"user_id": "alice"}, db)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(db.rollbacks, 1)


if __name__ == "__main__":
    unittest.main()
