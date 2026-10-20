"""Review production feedback, score sanitized cases, and compare releases.

This module deliberately never copies raw chat logs into a tracked dataset.
An operator must write and approve a redacted review record first.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected an object")
        result.append(value)
    return result


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _required_text(value: dict, key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be nonempty text")
    return item.strip()


def _validate_case(case: dict) -> None:
    case_id = _required_text(case, "case_id")
    if not CASE_ID.fullmatch(case_id):
        raise ValueError(f"invalid case_id: {case_id}")
    _required_text(case, "scenario")
    _required_text(case, "message")
    expected = case.get("expected")
    if not isinstance(expected, dict) or expected.get("status") not in (
        "done",
        "stopped",
        "error",
    ):
        raise ValueError(f"{case_id}: expected.status must be done, stopped, or error")
    for key in ("contains_all", "excludes_all"):
        phrases = expected.get(key, [])
        if not isinstance(phrases, list) or any(
            not isinstance(p, str) or not p for p in phrases
        ):
            raise ValueError(
                f"{case_id}: expected.{key} must be a list of nonempty strings"
            )


def promote_reviews(reviews_path: Path, dataset_path: Path) -> list[str]:
    """Append only explicitly approved, redacted cases; never ingest raw logs."""
    if not reviews_path.is_file():
        raise ValueError("review file does not exist")
    reviews = _records(reviews_path)
    dataset = _records(dataset_path)
    existing_ids = {}
    existing_feedback = {}
    for case in dataset:
        _validate_case(case)
        case_id = case["case_id"]
        if case_id in existing_ids:
            raise ValueError(f"duplicate case_id in dataset: {case_id}")
        existing_ids[case_id] = case.get("source_feedback_id")
        if case.get("source_feedback_id"):
            feedback_id = case["source_feedback_id"]
            if feedback_id in existing_feedback:
                raise ValueError(f"duplicate source_feedback_id in dataset: {feedback_id}")
            existing_feedback[feedback_id] = case_id

    promoted = []
    for review in reviews:
        if review.get("decision") != "approved":
            continue
        if review.get("redacted") is not True:
            raise ValueError("approved feedback must have redacted=true")
        feedback_id = _required_text(review, "feedback_id")
        case = {
            key: review[key]
            for key in ("case_id", "scenario", "message", "expected")
            if key in review
        }
        _validate_case(case)
        if (existing_feedback.get(feedback_id) == case["case_id"] and
                existing_ids.get(case["case_id"]) == feedback_id):
            continue
        if feedback_id in existing_feedback or case["case_id"] in existing_ids:
            raise ValueError(
                f"conflicting feedback or case ID: {feedback_id}, {case['case_id']}"
            )
        case["source_feedback_id"] = feedback_id
        dataset.append(case)
        promoted.append(case["case_id"])
        existing_ids[case["case_id"]] = feedback_id
        existing_feedback[feedback_id] = case["case_id"]

    if promoted:
        _write(
            dataset_path,
            "".join(
                json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n"
                for case in dataset
            ),
        )
    return promoted


def score_observations(
    dataset_path: Path, observations_path: Path, *, code_version: str, model: str
) -> dict:
    """Score one observation per case; return metadata and numeric outcomes only."""
    dataset = _records(dataset_path)
    observations = _records(observations_path)
    if not dataset:
        raise ValueError("dataset is empty")
    cases_by_id = {}
    for case in dataset:
        _validate_case(case)
        if case["case_id"] in cases_by_id:
            raise ValueError(f"duplicate dataset case ID: {case['case_id']}")
        cases_by_id[case["case_id"]] = case
    observed = {}
    for item in observations:
        case_id = _required_text(item, "case_id")
        if case_id in observed:
            raise ValueError(f"duplicate observation case ID: {case_id}")
        observed[case_id] = item
    if set(cases_by_id) != set(observed):
        raise ValueError("observation case IDs must exactly match dataset case IDs")

    results = []
    for case_id, case in cases_by_id.items():
        item = observed[case_id]
        answer = item.get("answer", "")
        if not isinstance(answer, str):
            raise ValueError(f"{case_id}: answer must be text")
        expected = case["expected"]
        checks = [item.get("status") == expected["status"]]
        folded = answer.casefold()
        checks.extend(
            phrase.casefold() in folded for phrase in expected.get("contains_all", [])
        )
        checks.extend(
            phrase.casefold() not in folded
            for phrase in expected.get("excludes_all", [])
        )
        result = {
            "case_id": case_id,
            "scenario": case["scenario"],
            "passed": all(checks),
        }
        for field in ("latency_ms", "input_tokens", "output_tokens"):
            value = item.get(field)
            if value is not None:
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or value < 0
                ):
                    raise ValueError(f"{case_id}: {field} must be nonnegative")
                result[field] = value
        results.append(result)
    return {
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "code_version": _required_text({"code_version": code_version}, "code_version"),
        "model": _required_text({"model": model}, "model"),
        "cases": results,
    }


def compare_runs(baseline: dict, current: dict) -> dict:
    if baseline.get("dataset_sha256") != current.get("dataset_sha256"):
        raise ValueError("cannot compare runs with different dataset versions")
    old = {case["case_id"]: case for case in baseline["cases"]}
    new = {case["case_id"]: case for case in current["cases"]}
    if (
        len(old) != len(baseline["cases"])
        or len(new) != len(current["cases"])
        or set(old) != set(new)
    ):
        raise ValueError("run case IDs must match uniquely")
    if not old:
        raise ValueError("runs contain no cases")
    regressions = sorted(
        case_id
        for case_id in old
        if old[case_id]["passed"] and not new[case_id]["passed"]
    )
    improvements = sorted(
        case_id
        for case_id in old
        if not old[case_id]["passed"] and new[case_id]["passed"]
    )
    return {
        "dataset_sha256": baseline["dataset_sha256"],
        "baseline_pass_rate": sum(bool(case["passed"]) for case in old.values())
        / len(old),
        "current_pass_rate": sum(bool(case["passed"]) for case in new.values())
        / len(new),
        "regressions": regressions,
        "improvements": improvements,
    }


def parse_chat_sse(stream: str) -> dict:
    """Extract only the final status, answer, and provider usage from a chat stream."""
    final = None
    usage = {}
    normalized = stream.replace("\r\n", "\n")
    for frame in normalized.split("\n\n"):
        name = "message"
        data_lines = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid SSE data JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid SSE data object")
        if name == "usage":
            for field in ("input_tokens", "output_tokens"):
                if isinstance(payload.get(field), int) and payload[field] >= 0:
                    usage[field] = payload[field]
        elif name in ("done", "stopped"):
            if not isinstance(payload.get("answer"), str):
                raise ValueError("completion event has no answer")
            final = {"status": name, "answer": payload["answer"]}
        elif name == "error":
            final = {"status": "error", "answer": ""}
    if final is None:
        raise ValueError("SSE stream ended without a completion or error event")
    return {**final, **usage}


def run_dataset(dataset_path: Path, output_path: Path, *, base_url: str, timeout: float = 120) -> int:
    """Run one-turn cases against an isolated API account and keep answers private."""
    private_root = Path(__file__).resolve().parents[1] / "output"
    if private_root.is_symlink():
        raise ValueError("output/ must not be a symlink")
    if not output_path.resolve().is_relative_to(private_root.resolve()):
        raise ValueError("observations must be written under the ignored output/ directory")
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("base_url must be an HTTP(S) origin")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("bearer token requires HTTPS outside localhost")
    token = os.environ.get("EVAL_ACCESS_TOKEN")
    if not token:
        raise ValueError("EVAL_ACCESS_TOKEN is required")
    cases = _records(dataset_path)
    if not cases:
        raise ValueError("dataset is empty")
    observations = []
    for case in cases:
        _validate_case(case)
        body = json.dumps({
            "message": case["message"],
            "session_id": f"eval-{uuid.uuid4()}",
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            base_url.rstrip("/") + "/chat",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=timeout) as response:
            observation = parse_chat_sse(response.read().decode("utf-8"))
        observation["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        observations.append({"case_id": case["case_id"], **observation})
    _write(output_path, "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in observations))
    return len(observations)


def _comparison_markdown(report: dict, baseline: dict, current: dict) -> str:
    regressions = ", ".join(report["regressions"]) or "无"
    improvements = ", ".join(report["improvements"]) or "无"
    return (
        "# 评测基线对比\n\n"
        f"- 数据集 SHA-256：`{report['dataset_sha256']}`\n"
        f"- 基线：`{baseline.get('code_version', 'unknown')}` / `{baseline.get('model', 'unknown')}`，通过率 {report['baseline_pass_rate']:.1%}\n"
        f"- 当前：`{current.get('code_version', 'unknown')}` / `{current.get('model', 'unknown')}`，通过率 {report['current_pass_rate']:.1%}\n"
        f"- 回退样本：{regressions}\n"
        f"- 改善样本：{improvements}\n"
    )


async def export_feedback(path: Path, *, limit: int = 500, offset: int = 0) -> int:
    """Export a local, unreviewed copy for an operator; keep it out of Git."""
    if limit < 1 or limit > 5000 or offset < 0:
        raise ValueError("limit must be 1..5000 and offset must be nonnegative")
    private_root = Path(__file__).resolve().parents[1] / "output"
    if private_root.is_symlink():
        raise ValueError("output/ must not be a symlink")
    if not path.resolve().is_relative_to(private_root.resolve()):
        raise ValueError("feedback export must be written under the ignored output/ directory")
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    from sqlalchemy import select

    from api.db import AsyncSessionLocal
    from api.models import ChatFeedback, ChatLog

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatFeedback, ChatLog)
                .join(ChatLog, ChatLog.id == ChatFeedback.chat_log_id)
                .order_by(ChatFeedback.created_at, ChatFeedback.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    records = [
        {
            "feedback_id": str(feedback.id),
            "category": feedback.category,
            "decision": "pending",
            "redacted": False,
            "case_id": "",
            "scenario": feedback.category,
            "message": chat_log.message,
            "observed_answer": chat_log.answer,
            "expected": {"status": "done", "contains_all": []},
        }
        for feedback, chat_log in rows
    ]
    _write(path, "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records))
    return len(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Export private pending feedback for manual review")
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--limit", type=int, default=500)
    export.add_argument("--offset", type=int, default=0)
    run = commands.add_parser("run", help="Run cases against an isolated API account")
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--base-url", required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--timeout", type=float, default=120)
    promote = commands.add_parser("promote", help="Promote approved, redacted feedback")
    promote.add_argument("--reviews", type=Path, required=True)
    promote.add_argument("--dataset", type=Path, required=True)
    score = commands.add_parser("score", help="Score exported run observations")
    score.add_argument("--dataset", type=Path, required=True)
    score.add_argument("--observations", type=Path, required=True)
    score.add_argument("--code-version", required=True)
    score.add_argument("--model", required=True)
    score.add_argument("--out", type=Path, required=True)
    compare = commands.add_parser(
        "compare", help="Compare exact case sets with a baseline"
    )
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--current", type=Path, required=True)
    compare.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "export":
        count = asyncio.run(export_feedback(args.out, limit=args.limit, offset=args.offset))
        print(f"exported {count} private review records: {args.out}")
        return 0
    if args.command == "run":
        count = run_dataset(args.dataset, args.out, base_url=args.base_url, timeout=args.timeout)
        print(f"ran {count} isolated cases: {args.out}")
        return 0
    if args.command == "promote":
        print(
            json.dumps(
                {"promoted": promote_reviews(args.reviews, args.dataset)},
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "score":
        result = score_observations(
            args.dataset,
            args.observations,
            code_version=args.code_version,
            model=args.model,
        )
        _write(args.out, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(f"scored {len(result['cases'])} cases: {args.out}")
        return 0
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    current = json.loads(args.current.read_text(encoding="utf-8"))
    report = compare_runs(baseline, current)
    _write(args.out, _comparison_markdown(report, baseline, current))
    print(f"regressions: {len(report['regressions'])}; report: {args.out}")
    return 1 if report["regressions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
