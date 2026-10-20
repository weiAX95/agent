"""Offline regression tests for long-term memory writes.

main.py and tools.py initialize the model and Milvus at import time, so these tests
load only the functions under test and supply in-memory dependencies.
"""

import ast
import contextlib
import copy
import datetime
import io
import json
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, TypeAlias

from pydantic import BaseModel
from chat_graph_loader import attach_chat_workflow


ROOT = Path(__file__).resolve().parents[1]


def load_definitions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = [
        node
        for node in tree.body
        if (
            isinstance(node, (ast.FunctionDef, ast.ClassDef))
            and node.name in names
        )
        or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in names
        )
    ]
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)


class MemoryExtractionGateTests(unittest.TestCase):
    def test_nonempty_messages_reach_model_without_keyword_or_question_filter(self):
        namespace = {}
        load_definitions(
            ROOT / "src/agent/main.py", {"should_extract_memory"}, namespace
        )
        should_extract = namespace["should_extract_memory"]
        for message in (
            "我叫张三", "OK。我叫张三。我喜欢吃西瓜", "以后叫我小张",
            "家里养了一只猫", "我叫张三，你记住了吗？",
            "帮我解释一下 Python 的 async", "我喜欢什么？", "额",
        ):
            with self.subTest(message=message):
                self.assertTrue(should_extract(message))
        self.assertFalse(should_extract(""))
        self.assertFalse(should_extract("  \n "))


class FakeMilvus:
    def __init__(self):
        self.records = {}
        self.queries = []
        self.inserts = []
        self.upserts = []

    def query(self, *, filter, output_fields, **kwargs):
        user_id, end = json.JSONDecoder().raw_decode(filter.removeprefix("user_id == "))
        remainder = filter.removeprefix("user_id == ")[end:]
        memory_keys = json.loads(remainder.removeprefix(" and memory_key in "))
        filter_params = {"user_id": user_id, "memory_keys": memory_keys}
        self.queries.append((filter, copy.deepcopy(filter_params)))
        keys = set(filter_params["memory_keys"])
        return [
            {field: record[field] for field in output_fields}
            for record in self.records.values()
            if record["user_id"] == filter_params["user_id"]
            and record["memory_key"] in keys
        ]

    def get(self, *, ids, output_fields, **kwargs):
        return [
            {field: self.records[memory_id][field] for field in output_fields}
            for memory_id in ids
            if memory_id in self.records
        ]

    def insert(self, *, data, **kwargs):
        for record in data:
            if record["id"] in self.records:
                raise ValueError("duplicate ID")
            self.records[record["id"]] = copy.deepcopy(record)
            self.inserts.append(copy.deepcopy(record))

    def upsert(self, *, data, **kwargs):
        for record in data:
            self.records[record["id"]] = copy.deepcopy(record)
            self.upserts.append(copy.deepcopy(record))


class FakeEmbedding:
    def __init__(self):
        self.calls = []

    def embed_query(self, memory):
        self.calls.append(memory)
        return [float(len(memory))]


class FakeResolver:
    def __init__(self):
        self.batch = None
        self.prompts = []

    def invoke(self, prompt, config=None):
        self.prompts.append(prompt)
        if self.batch is None:
            raise AssertionError("unexpected resolver call")
        return self.batch


class MemoryUpdateTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeMilvus()
        self.embedding = FakeEmbedding()
        self.resolver = FakeResolver()
        self.tools = {
            "MEMORY_COLLECTION": "agent_memory",
            "client": self.client,
            "embed_model": self.embedding,
            "uuid": uuid,
            "json": json,
        }
        load_definitions(
            ROOT / "src/agent/tools.py",
            {"_get_memories_by_keys", "_insert_memory", "replace_memory"},
            self.tools,
        )
        self.flow = {
            "BaseModel": BaseModel,
            "Literal": Literal,
            "TypeAlias": TypeAlias,
            "datetime": datetime,
            "resolution_llm": self.resolver,
            "_get_memories_by_keys": self.tools["_get_memories_by_keys"],
            "_recall_memories": lambda **kwargs: [],
            "replace_memory": self.tools["replace_memory"],
            "route_memory_keys": lambda message: [],
            "route_memory_keys_with_llm": lambda message, callbacks=None: SimpleNamespace(
                should_recall=True, memory_keys=[]
            ),
            "should_recall_memory": lambda message: False,
            # These cases explicitly exercise memory writes; ordinary user turns
            # are covered by the extraction-gate regression tests.
            "should_extract_memory": lambda message: True,
            "make_thread_id": lambda user_id, session_id: f"{user_id}:{session_id}",
        }
        self.agent = SimpleNamespace(
            invoke=lambda *args, **kwargs: {
                "messages": [SimpleNamespace(content="回答")]
            }
        )
        load_definitions(
            ROOT / "src/agent/main.py",
            {
                "MemoryKey",
                "MemoryResolutionItem",
                "MemoryResolutionBatch",
                "MemoryCandidateItem",
                "MemoryCandidate",
                "resolve_memories",
                "validate_memory_resolutions",
                "_prepare_chat",
                "chat",
            },
            self.flow,
        )
        self.flow["save_to_memory"] = SimpleNamespace(
            invoke=lambda values: self.tools["_insert_memory"](**values)
        )
        attach_chat_workflow(self.flow)

    def add_record(self, memory_id, user, key, memory):
        self.client.records[memory_id] = {
            "id": memory_id,
            "user_id": user,
            "memory_key": key,
            "memory": memory,
            "vector": [1.0],
        }

    def candidates(self, *items):
        model = self.flow["MemoryCandidateItem"]
        self.flow["judge_memory"] = lambda message, callbacks=None: self.flow["MemoryCandidate"](
            memories=[model(memory_key=key, memory=memory) for key, memory in items]
        )

    def decisions(self, *items):
        model = self.flow["MemoryResolutionItem"]
        self.resolver.batch = self.flow["MemoryResolutionBatch"](
            resolutions=[model(**item) for item in items]
        )

    def chat(self, message="用户本轮消息", session_id="session-1", user_id="user-a"):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.flow["chat"](message, session_id, user_id, self.agent)

    def enable_real_extraction(self, memories):
        # Only the external model is replaced; run the real gate and judge node.
        self.candidate_prompts = []

        def invoke(prompt, config=None):
            self.candidate_prompts.append(prompt)
            return self.flow["MemoryCandidate"].model_validate({"memories": memories})

        self.flow["candidate_llm"] = SimpleNamespace(invoke=invoke)
        load_definitions(
            ROOT / "src/agent/main.py",
            {"should_extract_memory", "judge_memory"}, self.flow,
        )

    def test_name_and_food_are_saved_then_recalled_in_another_session(self):
        self.enable_real_extraction([
            {"memory_key": "user_name", "memory": "用户叫张三"},
            {"memory_key": "food_preference", "memory": "用户喜欢吃西瓜"},
        ])
        self.assertEqual(self.chat("OK。我叫张三。我喜欢吃西瓜"), "回答")
        self.assertEqual(
            {(row["memory_key"], row["memory"]) for row in self.client.records.values()},
            {("user_name", "用户叫张三"), ("food_preference", "用户喜欢吃西瓜")},
        )
        self.assertEqual(len(self.embedding.calls), 2)
        self.assertEqual(len(self.candidate_prompts), 1)

        load_definitions(
            ROOT / "src/agent/main.py",
            {"should_recall_memory", "route_memory_keys"}, self.flow,
        )
        self.enable_real_extraction([])
        prompts = []
        self.agent.invoke = lambda payload, **kwargs: (
            prompts.append(kwargs["context"]["system_prompt"])
            or {"messages": [SimpleNamespace(content="回答")]}
        )
        self.chat("我叫什么", session_id="new-session")
        self.assertIn("用户叫张三", prompts[-1])
        self.chat("我叫什么", session_id="new-session", user_id="user-b")
        self.assertNotIn("用户叫张三", prompts[-1])
        self.assertEqual(len(self.client.inserts), 2)

    def test_name_duplicate_and_correction_preserve_other_users_and_preferences(self):
        self.add_record("name-a", "user-a", "user_name", "用户叫张三")
        self.add_record("name-b", "user-b", "user_name", "用户叫王五")
        self.add_record("food-a", "user-a", "food_preference", "用户喜欢吃西瓜")
        self.enable_real_extraction([{"memory_key": "user_name", "memory": "用户叫张三"}])
        self.decisions({"memory_key": "user_name", "action": "none"})
        self.chat("我叫张三")
        self.assertEqual(len(self.client.records), 3)
        self.assertEqual(self.client.inserts, [])
        self.enable_real_extraction([{"memory_key": "user_name", "memory": "用户改名叫李四"}])
        self.decisions({"memory_key": "user_name", "action": "replace", "memory_id": "name-a", "memory": "用户叫李四"})
        self.chat("我改名叫李四了")
        self.assertEqual(self.client.records["name-a"]["memory"], "用户叫李四")
        self.assertEqual(self.client.records["name-b"]["memory"], "用户叫王五")
        self.assertEqual(self.client.records["food-a"]["memory"], "用户喜欢吃西瓜")
        self.assertEqual(len(self.client.upserts), 1)

    def test_preferred_name_and_background_have_independent_categories(self):
        self.enable_real_extraction([
            {"memory_key": "preferred_name", "memory": "用户希望被称为小张"},
            {"memory_key": "personal_background", "memory": "用户养了一只猫"},
        ])
        self.chat("以后叫我小张，家里养了一只猫")
        self.assertEqual({row["memory_key"] for row in self.client.inserts}, {"preferred_name", "personal_background"})

    def test_empty_model_result_does_not_store_questions_quotes_or_chitchat(self):
        self.enable_real_extraction([])
        for message in ("额", "张三是谁？", "我叫什么", "假如我叫张三", "他说：我叫张三"):
            with self.subTest(message=message):
                self.chat(message)
        self.assertEqual(len(self.candidate_prompts), 5)
        self.assertEqual(self.client.queries, [])
        self.assertEqual(self.client.inserts, [])
        self.assertEqual(self.embedding.calls, [])

    def test_query_escapes_literals_and_preserves_user_scope(self):
        user = 'user"\\特殊'
        key = 'current_job"\\'
        self.add_record("selected", user, key, "目标记忆")
        self.add_record("other", "other-user", key, "其他用户记忆")
        records = self.tools["_get_memories_by_keys"](user, [key, key])
        self.assertEqual([record["id"] for record in records], ["selected"])
        self.assertEqual(self.client.queries[-1][1]["memory_keys"], [key])

    def test_replaces_selected_old_id_and_inserts_new_key_after_one_decision(self):
        self.add_record("legacy-fixed-id", "user-a", "career_direction", "前端")
        self.add_record("other-user-id", "user-b", "career_direction", "后端")
        self.candidates(
            ("career_direction", "转向全栈"),
            ("food_preference", "喜欢茶"),
        )
        self.decisions(
            {
                "memory_key": "career_direction",
                "action": "replace",
                "memory_id": "legacy-fixed-id",
                "memory": "用户转向全栈开发",
            }
        )

        self.assertEqual(self.chat(), "回答")
        self.assertEqual(len(self.resolver.prompts), 1)
        self.assertIn("legacy-fixed-id", self.resolver.prompts[0])
        self.assertNotIn("other-user-id", self.resolver.prompts[0])
        self.assertEqual(self.client.records["legacy-fixed-id"]["memory"], "用户转向全栈开发")
        self.assertEqual(self.client.records["other-user-id"]["memory"], "后端")
        self.assertEqual(len(self.client.upserts), 1)
        self.assertEqual(len(self.client.inserts), 1)
        self.assertEqual(self.client.inserts[0]["memory_key"], "food_preference")
        self.assertNotEqual(self.client.inserts[0]["id"], "legacy-fixed-id")

    def test_same_key_independent_fact_insert_and_duplicate_none(self):
        self.add_record("old-tea", "user-a", "food_preference", "喜欢茶")
        self.candidates(("food_preference", "喜欢咖啡"))
        self.decisions({"memory_key": "food_preference", "action": "insert"})
        self.chat()

        self.assertEqual(len(self.client.records), 2)
        self.assertEqual(self.client.records["old-tea"]["memory"], "喜欢茶")
        self.assertNotEqual(self.client.inserts[0]["id"], "old-tea")

        self.candidates(("food_preference", "喜欢茶"))
        self.decisions({"memory_key": "food_preference", "action": "none"})
        self.chat()
        self.assertEqual(len(self.client.records), 2)
        self.assertEqual(len(self.client.inserts), 1)
        self.assertEqual(len(self.client.upserts), 0)

    def test_reversed_batch_results_match_by_key(self):
        self.add_record("career-id", "user-a", "career_direction", "前端")
        self.add_record("food-id", "user-a", "food_preference", "喜欢茶")
        self.candidates(("career_direction", "全栈"), ("food_preference", "喜欢咖啡"))
        self.decisions(
            {"memory_key": "food_preference", "action": "replace", "memory_id": "food-id", "memory": "喜欢咖啡"},
            {"memory_key": "career_direction", "action": "replace", "memory_id": "career-id", "memory": "全栈"},
        )
        self.chat()
        self.assertEqual(self.client.records["career-id"]["memory"], "全栈")
        self.assertEqual(self.client.records["food-id"]["memory"], "喜欢咖啡")

    def test_replacement_preserves_other_record_in_same_key(self):
        self.add_record("tea-id", "user-a", "food_preference", "喜欢茶")
        self.add_record("coffee-id", "user-a", "food_preference", "喜欢咖啡")
        self.candidates(("food_preference", "不再喜欢茶"))
        self.decisions(
            {
                "memory_key": "food_preference",
                "action": "replace",
                "memory_id": "tea-id",
                "memory": "不再喜欢茶",
            }
        )
        self.chat()
        self.assertEqual(self.client.records["tea-id"]["memory"], "不再喜欢茶")
        self.assertEqual(self.client.records["coffee-id"]["memory"], "喜欢咖啡")
        self.assertEqual(len(self.client.records), 2)

    def test_vector_recall_does_not_hide_other_records_in_same_key(self):
        self.add_record("tea-id", "user-a", "food_preference", "喜欢茶")
        self.add_record("coffee-id", "user-a", "food_preference", "喜欢咖啡")
        self.flow["should_recall_memory"] = lambda message: True
        self.flow["_recall_memories"] = lambda **kwargs: [
            {"id": "tea-id", "memory_key": "food_preference", "memory": "喜欢茶"}
        ]
        self.candidates(("food_preference", "不再喜欢咖啡"))
        self.decisions({"memory_key": "food_preference", "action": "none"})
        self.chat()
        self.assertIn("tea-id", self.resolver.prompts[0])
        self.assertIn("coffee-id", self.resolver.prompts[0])
        self.assertEqual(len(self.client.queries), 1)

    def test_invalid_batch_never_partially_writes(self):
        self.add_record("career-id", "user-a", "career_direction", "前端")
        self.add_record("other-user-id", "user-b", "career_direction", "后端")
        self.candidates(("food_preference", "喜欢茶"), ("career_direction", "全栈"))
        bad_batches = [
            (),
            (
                {"memory_key": "career_direction", "action": "none"},
                {"memory_key": "career_direction", "action": "none"},
            ),
            ({"memory_key": "food_preference", "action": "none"},),
            ({"memory_key": "career_direction", "action": "replace", "memory_id": "other-user-id", "memory": "全栈"},),
            ({"memory_key": "career_direction", "action": "replace", "memory_id": "career-id", "memory": "   "},),
        ]
        for decisions in bad_batches:
            with self.subTest(decisions=decisions):
                self.decisions(*decisions)
                with self.assertLogs("src.agent.chat_workflow", level="ERROR") as logs:
                    self.assertEqual(self.chat(), "回答")
                self.assertIn("Chat memory stage failed: resolve", logs.output[0])
                self.assertEqual(self.client.inserts, [])
                self.assertEqual(self.client.upserts, [])

    def test_empty_candidate_skips_database_and_resolver(self):
        self.candidates()
        self.assertEqual(self.chat(), "回答")
        self.assertEqual(self.client.queries, [])
        self.assertEqual(self.resolver.prompts, [])
        self.assertEqual(self.client.inserts, [])

    def test_replace_tool_rejects_wrong_target_without_writing(self):
        self.add_record("owned", "user-a", "career_direction", "前端")
        self.add_record("foreign", "user-b", "career_direction", "后端")
        replace = self.tools["replace_memory"]
        for memory_id, key in [
            ("missing", "career_direction"),
            ("foreign", "career_direction"),
            ("owned", "food_preference"),
        ]:
            with self.subTest(memory_id=memory_id, key=key):
                with self.assertRaises(ValueError):
                    replace("user-a", "新内容", key, memory_id)
        self.assertEqual(self.client.upserts, [])
        self.assertEqual(self.embedding.calls, [])

    def test_graph_passes_validated_target_id(self):
        self.add_record("uuid-old-id", "user-a", "career_direction", "前端")
        graph_namespace = {
            "_insert_memory": self.tools["_insert_memory"],
            "replace_memory": self.tools["replace_memory"],
            "AgentState": dict,
        }
        load_definitions(
            ROOT / "src/agent/graph.py", {"persist_memory_node"}, graph_namespace
        )
        state = {
            "user_id": "user-a",
            "memory_candidate": {"memory_key": "career_direction", "memory": "全栈"},
            "memory_resolution": {"action": "replace", "memory_id": "uuid-old-id"},
            "related_memories": [{"id": "uuid-old-id"}],
        }
        with contextlib.redirect_stdout(io.StringIO()):
            graph_namespace["persist_memory_node"](state)
        self.assertEqual(self.client.records["uuid-old-id"]["memory"], "全栈")
        self.assertEqual(len(self.client.records), 1)


if __name__ == "__main__":
    unittest.main()
