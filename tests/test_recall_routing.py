"""Offline regressions for recall gating, routing, and prompt injection.

Load the real routing functions without importing main.py, whose module-level
initialization loads models and connects to Milvus. Only model responses and
storage calls are replaced, so these tests exercise the actual routing logic.
"""

import ast
import contextlib
import datetime
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, TypeAlias
from unittest.mock import Mock

from pydantic import BaseModel


ROOT = Path(__file__).resolve().parents[1]


def load_routing(namespace):
    path = ROOT / "src/agent/main.py"
    names = {
        "MemoryKey",
        "MemoryRecallDecision",
        "should_recall_memory",
        "route_memory_keys",
        "route_memory_keys_with_llm",
        "_prepare_chat",
        "_update_long_term_memory",
        "chat",
    }
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
    exec(
        compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"),
        namespace,
    )


class RecallRoutingTests(unittest.TestCase):
    def setUp(self):
        self.exact_query = Mock(return_value=[])
        self.vector_query = Mock(return_value=[])
        self.router_model = SimpleNamespace(
            invoke=Mock(side_effect=AssertionError("unexpected router model call"))
        )
        self.agent = SimpleNamespace(
            invoke=Mock(return_value={"messages": [SimpleNamespace(content="回答")]})
        )
        self.namespace = {
            "BaseModel": BaseModel,
            "Literal": Literal,
            "TypeAlias": TypeAlias,
            "datetime": datetime,
            "recall_llm": self.router_model,
            "_get_memories_by_keys": self.exact_query,
            "_recall_memories": self.vector_query,
            "judge_memory": lambda message: SimpleNamespace(memories=[]),
            "should_extract_memory": lambda message: False,
            "make_thread_id": lambda user_id, session_id: "offline-thread",
        }
        load_routing(self.namespace)

    def router_decision(self, should_recall, keys):
        decision = self.namespace["MemoryRecallDecision"](
            should_recall=should_recall,
            memory_keys=keys,
        )
        self.router_model.invoke.side_effect = None
        self.router_model.invoke.return_value = decision

    def chat(self, message, expected_memory=None):
        with contextlib.redirect_stdout(io.StringIO()):
            answer = self.namespace["chat"](
                message, "session-a", "user-a", self.agent
            )

        self.assertEqual(answer, "回答")
        self.agent.invoke.assert_called_once()
        payload = self.agent.invoke.call_args.args[0]
        self.assertEqual(
            payload,
            {"messages": [{"role": "user", "content": message}]},
        )
        prompt = self.agent.invoke.call_args.kwargs["context"]["system_prompt"]
        self.assertIn("user-a", prompt)
        if expected_memory is None:
            self.assertIn("暂无相关长期记忆。", prompt)
        else:
            self.assertIn(expected_memory, prompt)
            self.assertNotIn("暂无相关长期记忆。", prompt)

    def test_general_career_question_does_not_query_personal_memory(self):
        message = "职业方向通常有哪些？"
        # A category mention alone does not ask for this user's history.
        self.assertEqual(self.namespace["route_memory_keys"](message), [])
        self.assertFalse(self.namespace["should_recall_memory"](message))

        self.chat(message)

        self.router_model.invoke.assert_not_called()
        self.exact_query.assert_not_called()
        self.vector_query.assert_not_called()

    def test_llm_veto_does_not_fall_back_to_vector_search(self):
        # should_recall remains authoritative even if the model also emits a key.
        self.router_decision(False, ["learning_direction"])

        self.chat("我想学习二叉树")

        self.router_model.invoke.assert_called_once()
        self.exact_query.assert_not_called()
        self.vector_query.assert_not_called()

    def test_unknown_category_uses_one_vector_search(self):
        self.router_decision(True, [])
        memory = "用户有多年的软件开发经验"
        self.vector_query.return_value = [
            {"memory_key": "current_job", "memory": memory}
        ]
        message = "根据我的背景，给些建议"

        self.chat(message, expected_memory=memory)

        self.router_model.invoke.assert_called_once()
        self.exact_query.assert_not_called()
        self.vector_query.assert_called_once_with(
            user_id="user-a", query=message, limit=5, min_score=0.4
        )

    def test_explicit_recollection_without_topic_reaches_vector_search(self):
        message = "你还记得我吗？"
        self.assertTrue(self.namespace["should_recall_memory"](message))
        self.assertTrue(self.namespace["should_recall_memory"]("我之前说过什么？"))
        self.assertEqual(self.namespace["route_memory_keys"](message), [])
        self.router_decision(True, [])
        memory = "用户长期维护个人知识库项目"
        self.vector_query.return_value = [
            {"memory_key": "long_term_project", "memory": memory}
        ]

        self.chat(message, expected_memory=memory)

        self.router_model.invoke.assert_called_once()
        self.exact_query.assert_not_called()
        self.vector_query.assert_called_once_with(
            user_id="user-a", query=message, limit=5, min_score=0.4
        )

    def test_first_person_general_career_question_obeys_llm_veto(self):
        message = "我想了解职业方向有哪些？"
        self.assertTrue(self.namespace["should_recall_memory"](message))
        self.assertEqual(self.namespace["route_memory_keys"](message), [])
        for other_general_question in (
            "我的问题是职业方向有哪些？",
            "根据我的理解，职业方向有哪些？",
            "我们团队的职业方向有哪些？",
        ):
            self.assertEqual(
                self.namespace["route_memory_keys"](other_general_question), []
            )
        self.router_decision(False, [])

        self.chat(message)

        self.router_model.invoke.assert_called_once()
        self.exact_query.assert_not_called()
        self.vector_query.assert_not_called()

    def test_explicit_category_rule_uses_one_exact_query(self):
        memory = "用户现在是软件工程师"
        self.exact_query.return_value = [
            {"memory_key": "current_job", "memory": memory}
        ]
        self.assertEqual(
            self.namespace["route_memory_keys"]("我的职业方向是什么？"),
            ["career_direction"],
        )

        self.chat("我现在做什么工作？", expected_memory=memory)

        self.router_model.invoke.assert_not_called()
        self.exact_query.assert_called_once_with(
            user_id="user-a", memory_keys=["current_job"]
        )
        self.vector_query.assert_not_called()

    def test_llm_known_category_uses_one_exact_query(self):
        self.router_decision(True, ["food_preference"])
        memory = "用户喜欢清淡饮食"
        self.exact_query.return_value = [
            {"memory_key": "food_preference", "memory": memory}
        ]

        self.chat("我喜欢吃什么？", expected_memory=memory)

        self.router_model.invoke.assert_called_once()
        self.exact_query.assert_called_once_with(
            user_id="user-a", memory_keys=["food_preference"]
        )
        self.vector_query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
