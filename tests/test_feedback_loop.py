"""The review gate and per-case baseline comparison work without external services."""

import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.feedback_loop import compare_runs, export_feedback, parse_chat_sse, promote_reviews, run_dataset, score_observations


class FeedbackLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_jsonl(self, name, records):
        path = self.root / name
        path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records))
        return path

    def test_only_approved_redacted_feedback_can_enter_dataset(self):
        reviews = self.write_jsonl(
            "reviews.jsonl",
            [
                {
                    "feedback_id": "feedback-1",
                    "decision": "approved",
                    "redacted": True,
                    "case_id": "recall-001",
                    "scenario": "recall",
                    "message": "我之前说过喜欢茶，是什么？",
                    "expected": {"status": "done", "contains_all": ["茶"]},
                },
                {
                    "feedback_id": "feedback-2",
                    "decision": "rejected",
                    "redacted": True,
                    "case_id": "rejected-001",
                    "scenario": "recall",
                    "message": "不应进入数据集",
                    "expected": {"status": "done"},
                },
            ],
        )
        dataset = self.root / "dataset.jsonl"
        self.assertEqual(promote_reviews(reviews, dataset), ["recall-001"])
        cases = [json.loads(line) for line in dataset.read_text().splitlines()]
        self.assertEqual([case["case_id"] for case in cases], ["recall-001"])
        self.assertEqual(promote_reviews(reviews, dataset), [])

        conflicting = self.write_jsonl(
            "conflicting.jsonl",
            [{**json.loads(reviews.read_text().splitlines()[0]), "feedback_id": "feedback-3"}],
        )
        with self.assertRaisesRegex(ValueError, "conflicting"):
            promote_reviews(conflicting, dataset)

        unredacted = self.write_jsonl(
            "unredacted.jsonl",
            [{**json.loads(reviews.read_text().splitlines()[0]), "redacted": False}],
        )
        with self.assertRaisesRegex(ValueError, "redacted"):
            promote_reviews(unredacted, dataset)
        self.assertEqual(len(dataset.read_text().splitlines()), 1)

    def test_compare_finds_regressed_case_even_when_pass_rate_is_unchanged(self):
        baseline = {
            "dataset_sha256": "same-dataset",
            "cases": [
                {"case_id": "a", "passed": True, "latency_ms": 100},
                {"case_id": "b", "passed": False, "latency_ms": 150},
            ],
        }
        current = {
            "dataset_sha256": "same-dataset",
            "cases": [
                {"case_id": "a", "passed": False, "latency_ms": 120},
                {"case_id": "b", "passed": True, "latency_ms": 140},
            ],
        }
        report = compare_runs(baseline, current)
        self.assertEqual(report["regressions"], ["a"])
        self.assertEqual(report["improvements"], ["b"])
        self.assertEqual(report["baseline_pass_rate"], report["current_pass_rate"])
        with self.assertRaisesRegex(ValueError, "dataset"):
            compare_runs(baseline, {**current, "dataset_sha256": "different"})

    def test_score_observations_requires_complete_matching_case_set(self):
        dataset = self.write_jsonl(
            "dataset.jsonl",
            [
                {"case_id": "a", "scenario": "answer", "message": "问候", "expected": {"status": "done", "contains_all": ["你好"], "excludes_all": ["秘密"]}},
                {"case_id": "b", "scenario": "answer", "message": "拒答", "expected": {"status": "done", "contains_all": ["不知道"]}},
            ],
        )
        observations = self.write_jsonl(
            "observations.jsonl",
            [
                {"case_id": "a", "status": "done", "answer": "你好", "latency_ms": 100, "input_tokens": 3, "output_tokens": 2},
                {"case_id": "b", "status": "done", "answer": "猜一个", "latency_ms": 200},
            ],
        )
        result = score_observations(dataset, observations, code_version="v1", model="fake")
        self.assertEqual([(c["case_id"], c["passed"]) for c in result["cases"]], [("a", True), ("b", False)])
        self.assertEqual(result["cases"][0]["input_tokens"], 3)
        self.assertNotIn("answer", result["cases"][0])
        missing = self.write_jsonl("missing.jsonl", [{"case_id": "a", "status": "done", "answer": "你好"}])
        with self.assertRaisesRegex(ValueError, "case IDs"):
            score_observations(dataset, missing, code_version="v1", model="fake")

    def test_raw_feedback_export_cannot_write_into_tracked_dataset(self):
        with self.assertRaisesRegex(ValueError, "ignored output"):
            asyncio.run(export_feedback(self.root / "dataset.jsonl"))

    def test_sse_observation_extracts_final_answer_and_usage(self):
        stream = (
            ': connected\n\n'
            'event: token\ndata: {"content":"你"}\n\n'
            'event: usage\ndata: {"input_tokens":4,"output_tokens":2}\n\n'
            'event: done\ndata: {"answer":"你好","chat_log_id":"private"}\n\n'
        )
        self.assertEqual(parse_chat_sse(stream), {"status": "done", "answer": "你好", "input_tokens": 4, "output_tokens": 2})
        with self.assertRaisesRegex(ValueError, "completion"):
            parse_chat_sse('event: token\ndata: {"content":"partial"}\n\n')

    def test_run_uses_bearer_token_and_writes_observations_privately(self):
        dataset = self.write_jsonl("dataset.jsonl", [
            {"case_id": "one", "scenario": "answer", "message": "test", "expected": {"status": "done"}},
        ])
        output_root = Path(__file__).resolve().parents[1] / "output"
        output_root.mkdir(exist_ok=True)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b'event: done\ndata: {"answer":"ok"}\n\n'

        with tempfile.TemporaryDirectory(dir=output_root) as private_dir:
            out = Path(private_dir) / "observations.jsonl"
            with patch.dict("os.environ", {"EVAL_ACCESS_TOKEN": "test-only-token"}):
                with patch("tools.feedback_loop.urllib.request.urlopen", return_value=Response()) as send:
                    self.assertEqual(run_dataset(dataset, out, base_url="http://127.0.0.1:8001"), 1)
            request = send.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer test-only-token")
            self.assertEqual(json.loads(out.read_text())["answer"], "ok")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            run_dataset(dataset, output_root / "no-write.jsonl", base_url="http://example.org")


if __name__ == "__main__":
    unittest.main()
