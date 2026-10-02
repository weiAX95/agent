"""Offline regression tests for checkpoint routing and request context.

The production agent module initializes remote dependencies when imported. These
tests load only ``chat`` from it and supply a recording agent instead.
"""

import ast
import contextlib
import copy
import datetime
import io
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]


def load_function(path, name, namespace, *, strip_decorators=True):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    function = copy.deepcopy(function)
    if strip_decorators:
        function.decorator_list = []
    future_annotations = ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[future_annotations, function], type_ignores=[])
    )
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


class RecordingAgent:
    def __init__(self):
        self.calls = []

    def invoke(self, payload, **kwargs):
        self.calls.append((payload, kwargs))
        return {"messages": [SimpleNamespace(content="回答")]}


class CheckpointIdentityTests(unittest.TestCase):
    def test_thread_id_is_deterministic_and_isolates_users_and_sessions(self):
        from src.agent.checkpoint import make_thread_id

        self.assertEqual(make_thread_id("alice", "s1"), make_thread_id("alice", "s1"))
        self.assertNotEqual(make_thread_id("alice", "s1"), make_thread_id("bob", "s1"))
        self.assertNotEqual(make_thread_id("alice", "s1"), make_thread_id("alice", "s2"))
        # A plain ``user_id + ':' + session_id`` would collide for this pair.
        self.assertNotEqual(make_thread_id("a:b", "c"), make_thread_id("a", "b:c"))

    def test_checkpoint_lock_key_is_stable_scoped_and_signed_bigint(self):
        from src.agent.checkpoint import checkpoint_lock_id

        lock_id = checkpoint_lock_id("alice", "s1")
        self.assertEqual(lock_id, checkpoint_lock_id("alice", "s1"))
        self.assertNotEqual(lock_id, checkpoint_lock_id("bob", "s1"))
        self.assertNotEqual(lock_id, checkpoint_lock_id("alice", "s2"))
        self.assertNotEqual(checkpoint_lock_id("a:b", "c"), checkpoint_lock_id("a", "b:c"))
        self.assertGreaterEqual(lock_id, -(2**63))
        self.assertLess(lock_id, 2**63)

    def test_checkpoint_dsn_preserves_connection_fields_and_encoded_password(self):
        from src.agent.checkpoint import checkpoint_dsn

        database_url = (
            "postgresql+asyncpg://agent:p%40ss%3Aword@db.example:5432/memory"
            "?sslmode=require&application_name=agent"
        )
        original = make_url(database_url)
        converted = make_url(checkpoint_dsn(database_url))

        self.assertTrue(converted.drivername.startswith("postgresql"))
        self.assertNotIn("asyncpg", converted.drivername)
        for field in ("username", "password", "host", "port", "database", "query"):
            with self.subTest(field=field):
                self.assertEqual(getattr(converted, field), getattr(original, field))

    def test_checkpoint_dsn_rejects_other_database_backends(self):
        from src.agent.checkpoint import checkpoint_dsn

        with self.assertRaises(ValueError):
            checkpoint_dsn("sqlite:///local.db")


class ChatCheckpointTests(unittest.TestCase):
    def setUp(self):
        from src.agent.checkpoint import make_thread_id

        self.agent = RecordingAgent()
        self.namespace = {
            "datetime": datetime,
            "make_thread_id": make_thread_id,
            "route_memory_keys": lambda message: [],
            "route_memory_keys_with_llm": lambda message: [],
            "should_recall_memory": lambda message: False,
            "_get_memories_by_keys": lambda **kwargs: [],
            "_recall_memories": lambda **kwargs: [],
            "judge_memory": lambda message: SimpleNamespace(memories=[]),
        }
        self.chat = load_function(ROOT / "src/agent/main.py", "chat", self.namespace)

    def call_chat(self, user_id, session_id, message="你好"):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.chat(message, session_id, user_id, self.agent)

    def test_agent_receives_only_current_user_message_and_scoped_thread(self):
        from src.agent.checkpoint import make_thread_id

        self.assertEqual(self.call_chat("alice", "s1"), "回答")
        payload, kwargs = self.agent.calls[-1]
        self.assertEqual(payload, {"messages": [{"role": "user", "content": "你好"}]})
        self.assertEqual(
            kwargs["config"]["configurable"]["thread_id"],
            make_thread_id("alice", "s1"),
        )
        self.assertIn("system_prompt", kwargs["context"])
        self.assertIn("alice", kwargs["context"]["system_prompt"])

    def test_each_user_and_session_uses_its_own_checkpoint(self):
        for user_id, session_id in [
            ("alice", "shared"),
            ("bob", "shared"),
            ("alice", "second"),
            ("alice", "shared"),
        ]:
            self.call_chat(user_id, session_id)
        thread_ids = [
            kwargs["config"]["configurable"]["thread_id"]
            for _, kwargs in self.agent.calls
        ]
        self.assertEqual(thread_ids[0], thread_ids[3])
        self.assertEqual(len(set(thread_ids)), 3)

    def test_recalled_memory_stays_in_context_outside_checkpoint_messages(self):
        self.namespace["route_memory_keys"] = lambda message: ["current_job"]
        self.namespace["_get_memories_by_keys"] = lambda **kwargs: [
            {"memory_key": "current_job", "memory": "用户是软件工程师"}
        ]
        self.call_chat("alice", "s1")
        payload, kwargs = self.agent.calls[-1]
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertIn("用户是软件工程师", kwargs["context"]["system_prompt"])

    def test_debug_reads_the_same_scoped_checkpoint_as_chat(self):
        self.call_chat("alice", "s1")
        expected = self.agent.calls[-1][1]["config"]
        seen = []
        reader = SimpleNamespace(
            get_state=lambda config: (
                seen.append(config),
                SimpleNamespace(values={"messages": []}),
            )[1]
        )
        debug_memory = load_function(
            ROOT / "src/agent/main.py", "debug_memory", self.namespace
        )
        with contextlib.redirect_stdout(io.StringIO()):
            debug_memory("s1", "alice", reader)
        self.assertEqual(seen, [expected])

    def test_real_agent_uses_current_prompt_without_storing_system_messages(self):
        from langchain.agents import create_agent
        from langchain.agents.middleware import dynamic_prompt
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from langgraph.checkpoint.memory import InMemorySaver
        from pydantic import PrivateAttr

        class RecordingFakeModel(FakeListChatModel):
            _seen_messages: list = PrivateAttr(default_factory=list)

            def _call(self, messages, *args, **kwargs):
                self._seen_messages.append(copy.deepcopy(messages))
                return super()._call(messages, *args, **kwargs)

        prompt = load_function(
            ROOT / "src/agent/main.py",
            "conversation_prompt",
            {"dynamic_prompt": dynamic_prompt},
            strip_decorators=False,
        )
        model = RecordingFakeModel(responses=["第一轮回复", "第二轮回复"])
        agent = create_agent(
            model=model,
            tools=[],
            checkpointer=InMemorySaver(),
            middleware=[prompt],
        )
        config = {"configurable": {"thread_id": "alice:s1"}}

        first = agent.invoke(
            {"messages": [{"role": "user", "content": "第一轮用户"}]},
            config=config,
            context={"system_prompt": "系统提示-第一轮"},
        )
        self.assertEqual(first["messages"][-1].content, "第一轮回复")
        second = agent.invoke(
            {"messages": [{"role": "user", "content": "第二轮用户"}]},
            config=config,
            context={"system_prompt": "系统提示-第二轮"},
        )
        self.assertEqual(second["messages"][-1].content, "第二轮回复")

        self.assertEqual(len(model._seen_messages), 2)
        self.assertEqual(
            [message.type for message in model._seen_messages[1]],
            ["system", "human", "ai", "human"],
        )
        self.assertEqual(
            [message.content for message in model._seen_messages[1]],
            ["系统提示-第二轮", "第一轮用户", "第一轮回复", "第二轮用户"],
        )
        self.assertEqual(
            [message.type for message in agent.get_state(config).values["messages"]],
            ["human", "ai", "human", "ai"],
        )


class ApiThreadpoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_endpoint_locks_before_threadpool_and_commits_after(self):
        from src.agent.checkpoint import checkpoint_lock_id

        started_agent = object()
        chat_calls = []
        threadpool_calls = []
        events = []

        def fake_chat(*args, **kwargs):
            events.append("chat")
            chat_calls.append((args, kwargs))
            return "回答"

        async def fake_run_in_threadpool(func, *args, **kwargs):
            events.append("threadpool")
            threadpool_calls.append(func)
            return func(*args, **kwargs)

        class FakeDB:
            def __init__(self):
                self.added = []
                self.commits = 0
                self.executions = []

            async def execute(self, statement, params):
                events.append("lock")
                self.executions.append((statement, params))

            def add(self, value):
                events.append("add_log")
                self.added.append(value)

            async def commit(self):
                events.append("commit")
                self.commits += 1

        app = SimpleNamespace(state=SimpleNamespace(agent=started_agent))
        namespace = {
            "chat": fake_chat,
            "run_in_threadpool": fake_run_in_threadpool,
            "Depends": lambda dependency: None,
            "get_db": lambda: None,
            "ChatLog": lambda **values: SimpleNamespace(**values),
            "text": lambda statement: statement,
            "checkpoint_lock_id": checkpoint_lock_id,
        }
        endpoint = load_function(ROOT / "api/server.py", "chat_api", namespace)
        db = FakeDB()
        request = SimpleNamespace(message="你好", session_id="s1", user_id="alice")
        http_request = SimpleNamespace(app=app)

        with contextlib.redirect_stdout(io.StringIO()):
            result = await endpoint(request, http_request, db)

        self.assertEqual(result, {"answer": "回答"})
        self.assertEqual(events, ["lock", "threadpool", "chat", "add_log", "commit"])
        self.assertEqual(len(db.executions), 1)
        statement, params = db.executions[0]
        self.assertIn("pg_advisory_xact_lock", statement)
        self.assertEqual(params, {"lock_id": checkpoint_lock_id("alice", "s1")})
        self.assertEqual(threadpool_calls, [fake_chat])
        self.assertEqual(len(chat_calls), 1)
        args, kwargs = chat_calls[0]
        self.assertIn(started_agent, (*args, *kwargs.values()))
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.added[0].user_id, "alice")
        self.assertEqual(db.added[0].session_id, "s1")
        self.assertEqual(db.added[0].answer, "回答")


class LifespanTests(unittest.IsolatedAsyncioTestCase):
    def build_fixture(self, *, setup_fails=False):
        from src.agent.checkpoint import CHECKPOINT_SETUP_LOCK_ID

        events = []
        configured = {}
        agent = object()
        row_factory = object()

        class FakeConnection:
            async def execute(self, statement, params):
                events.append(("setup_lock", statement, params))

            async def run_sync(self, callback):
                events.append(("create_tables", callback))

        class FakeEngine:
            def begin(self):
                class BeginContext:
                    async def __aenter__(self):
                        events.append("engine_begin")
                        return FakeConnection()

                    async def __aexit__(self, exc_type, exc, tb):
                        events.append("engine_end")

                return BeginContext()

            async def dispose(self):
                events.append("engine_dispose")

        class FakePool:
            def __init__(self, **kwargs):
                configured.update(kwargs)
                events.append("pool_create")

            def open(self, *, wait):
                self.wait = wait
                events.append("pool_open")

            def close(self):
                events.append("pool_close")

        class FakeSaver:
            def __init__(self, pool):
                self.pool = pool
                events.append("saver_create")

            def setup(self):
                events.append("saver_setup")
                if setup_fails:
                    raise RuntimeError("setup failed")

        async def fake_run_in_threadpool(func, *args, **kwargs):
            events.append(("threadpool", func.__name__))
            return func(*args, **kwargs)

        def fake_build_agent(checkpointer):
            self.assertIsInstance(checkpointer, FakeSaver)
            self.assertIn("saver_setup", events)
            events.append("build_agent")
            return agent

        namespace = {
            "engine": FakeEngine(),
            "Base": SimpleNamespace(metadata=SimpleNamespace(create_all=object())),
            "ConnectionPool": FakePool,
            "PostgresSaver": FakeSaver,
            "run_in_threadpool": fake_run_in_threadpool,
            "build_agent": fake_build_agent,
            "settings": SimpleNamespace(database_url="postgresql+asyncpg://u:p@h/db"),
            "checkpoint_dsn": lambda url: "postgresql://u:p@h/db",
            "CHECKPOINT_SETUP_LOCK_ID": CHECKPOINT_SETUP_LOCK_ID,
            "dict_row": row_factory,
            "text": lambda statement: statement,
        }
        lifespan = load_function(ROOT / "api/server.py", "lifespan", namespace)
        app = SimpleNamespace(state=SimpleNamespace())
        return contextlib.asynccontextmanager(lifespan)(app), app, events, configured, agent, row_factory

    async def test_startup_sets_up_checkpoint_before_building_agent_and_closes_resources(self):
        from src.agent.checkpoint import CHECKPOINT_SETUP_LOCK_ID

        manager, app, events, configured, agent, row_factory = self.build_fixture()
        async with manager:
            self.assertIs(app.state.agent, agent)
            self.assertEqual(events.count("saver_setup"), 1)
            self.assertNotIn("pool_close", events)

        self.assertEqual(configured["conninfo"], "postgresql://u:p@h/db")
        event_names = [item[0] if isinstance(item, tuple) else item for item in events]
        setup_lock = next(item for item in events if isinstance(item, tuple) and item[0] == "setup_lock")
        self.assertIn("pg_advisory_xact_lock", setup_lock[1])
        self.assertEqual(
            setup_lock[2],
            {"lock_id": CHECKPOINT_SETUP_LOCK_ID},
        )
        self.assertLess(event_names.index("engine_begin"), event_names.index("setup_lock"))
        self.assertLess(event_names.index("setup_lock"), event_names.index("create_tables"))
        self.assertLess(event_names.index("create_tables"), event_names.index("pool_open"))
        self.assertLess(event_names.index("saver_setup"), event_names.index("engine_end"))
        self.assertLess(event_names.index("engine_end"), event_names.index("build_agent"))
        self.assertFalse(configured["open"])
        self.assertTrue(configured["kwargs"]["autocommit"])
        self.assertIs(configured["kwargs"]["row_factory"], row_factory)
        self.assertEqual(configured["kwargs"]["prepare_threshold"], 0)
        self.assertLess(events.index("pool_open"), events.index("saver_setup"))
        self.assertLess(events.index("saver_setup"), events.index("build_agent"))
        self.assertLess(events.index("build_agent"), events.index("pool_close"))
        self.assertEqual(events[-1], "engine_dispose")

    async def test_failed_checkpoint_setup_still_closes_pool_and_engine(self):
        manager, app, events, *_ = self.build_fixture(setup_fails=True)
        with self.assertRaisesRegex(RuntimeError, "setup failed"):
            async with manager:
                pass
        self.assertNotIn("build_agent", events)
        event_names = [item[0] if isinstance(item, tuple) else item for item in events]
        self.assertLess(event_names.index("setup_lock"), event_names.index("saver_setup"))
        self.assertLess(event_names.index("saver_setup"), event_names.index("engine_end"))
        self.assertIn("pool_close", events)
        self.assertEqual(events[-1], "engine_dispose")


if __name__ == "__main__":
    unittest.main()
