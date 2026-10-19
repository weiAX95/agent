#!/usr/bin/env python3
"""Launch Codex with DeepSeek when the ChatGPT Codex quota is exhausted."""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CODEX = os.environ.get(
    "CODEX_CLI_PATH",
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/"
    "CodexCLI.app/Contents/MacOS/codex",
)
PYTHON = ROOT / ".venv/bin/python"
KEY_FILE = Path.home() / ".config/codex-deepseek/env"
GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = int(os.environ.get("CODEX_DEEPSEEK_PORT", "18443"))


class QuotaReadError(RuntimeError):
    pass


def _read_json_response(proc: subprocess.Popen[str], request_id: int, timeout: float) -> dict[str, Any]:
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            events = selector.select(max(0.0, deadline - time.monotonic()))
            if not events:
                break
            line = proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                return message
    finally:
        selector.close()
    raise QuotaReadError(f"Codex app-server did not answer request {request_id}")


def read_limits(timeout: float = 12.0) -> dict[str, Any]:
    proc = subprocess.Popen(
        [CODEX, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert proc.stdin is not None

    def send(message: dict[str, Any]) -> None:
        proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    try:
        send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "codex_auto_fallback",
                        "title": "Codex Auto Fallback",
                        "version": "1.0.0",
                    }
                },
            }
        )
        initialized = _read_json_response(proc, 1, timeout)
        if initialized.get("error"):
            raise QuotaReadError(str(initialized["error"]))
        send({"method": "initialized", "params": {}})
        send({"method": "account/rateLimits/read", "id": 2})
        result = _read_json_response(proc, 2, timeout)
        if result.get("error"):
            raise QuotaReadError(str(result["error"]))
        return result.get("result") or {}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def _all_limit_buckets(limits: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = limits.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and by_id:
        return [value for value in by_id.values() if isinstance(value, dict)]
    single = limits.get("rateLimits")
    return [single] if isinstance(single, dict) else []


def quota_exhausted(limits: dict[str, Any], threshold: float = 100.0) -> bool:
    if limits.get("ordinaryUsageAllowed") is False or limits.get("spendControlReached") is True:
        return True
    buckets = _all_limit_buckets(limits)
    if not buckets:
        raise QuotaReadError("The account returned no Codex rate-limit bucket")
    for bucket in buckets:
        if bucket.get("rateLimitReachedType"):
            return True
        if bucket.get("spendControlReached") is True:
            return True
        for window_name in ("primary", "secondary"):
            window = bucket.get(window_name)
            if isinstance(window, dict) and float(window.get("usedPercent", 0)) >= threshold:
                return True
    return False


def format_status(limits: dict[str, Any]) -> str:
    lines: list[str] = []
    for bucket in _all_limit_buckets(limits):
        name = bucket.get("limitName") or bucket.get("limitId") or "codex"
        windows: list[str] = []
        for window_name in ("primary", "secondary"):
            window = bucket.get(window_name)
            if not isinstance(window, dict):
                continue
            used = float(window.get("usedPercent", 0))
            duration = int(window.get("windowDurationMins", 0))
            reset = window.get("resetsAt")
            reset_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(reset)) if reset else "unknown"
            windows.append(f"{window_name}: used {used:g}% ({duration} min), resets {reset_text}")
        lines.append(f"{name}: " + "; ".join(windows))
    return "\n".join(lines) or "No Codex quota information returned."


def save_key() -> None:
    key = getpass.getpass("DeepSeek API key: ").strip()
    if not key or "\n" in key or "\r" in key:
        raise SystemExit("A non-empty, single-line API key is required.")
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(f"DEEPSEEK_API_KEY={key}\n", encoding="utf-8")
    KEY_FILE.chmod(0o600)
    print(f"Saved DeepSeek key to {KEY_FILE} with mode 0600.")


def load_key() -> str | None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    try:
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        return None
    return None


def _wait_for_gateway(proc: subprocess.Popen[Any], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"DeepSeek gateway exited with status {proc.returncode}")
        try:
            connection = http.client.HTTPConnection(GATEWAY_HOST, GATEWAY_PORT, timeout=0.5)
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return
        except OSError as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"DeepSeek gateway did not become ready: {last_error}")


def run_deepseek(codex_args: list[str], reason: str = "Codex quota exhausted") -> int:
    key = load_key()
    if not key:
        print(
            "DeepSeek fallback is required, but no API key is configured.\n"
            "Run: codex-auto --setup-key",
            file=sys.stderr,
        )
        return 2
    if not PYTHON.exists():
        print(f"Missing gateway Python environment: {PYTHON}", file=sys.stderr)
        return 2

    gateway_token = secrets.token_urlsafe(32)
    env = os.environ.copy()
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
    loopback_hosts = ["127.0.0.1", "localhost"]
    no_proxy_parts = [part.strip() for part in no_proxy.split(",") if part.strip()]
    for host in loopback_hosts:
        if host not in no_proxy_parts:
            no_proxy_parts.append(host)
    no_proxy = ",".join(no_proxy_parts)
    env.update(
        {
            "DEEPSEEK_API_KEY": key,
            "CODEX_GATEWAY_API_KEY": gateway_token,
            "PYTHONPATH": str(ROOT),
            # macOS system proxies are not consistently bypassed by the Codex
            # HTTP client. Without these values, a loopback gateway can be sent
            # through a desktop proxy and return a misleading HTTP 502.
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }
    )
    gateway = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "tools.codex_deepseek.gateway:app",
            "--host",
            GATEWAY_HOST,
            "--port",
            str(GATEWAY_PORT),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
    )
    try:
        _wait_for_gateway(gateway)
        print(f"{reason}; switching to DeepSeek.", file=sys.stderr)
        child = subprocess.Popen([CODEX, "--profile", "deepseek", *codex_args], env=env)

        def forward(signum: int, _frame: Any) -> None:
            if child.poll() is None:
                child.send_signal(signum)

        old_handlers = {
            signum: signal.signal(signum, forward) for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            return child.wait()
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
    finally:
        gateway.terminate()
        try:
            gateway.wait(timeout=3)
        except subprocess.TimeoutExpired:
            gateway.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-auto",
        description="Use Codex normally and fall back to DeepSeek when quota is exhausted.",
    )
    parser.add_argument("--status", action="store_true", help="show Codex quota and exit")
    parser.add_argument("--setup-key", action="store_true", help="securely save the DeepSeek API key")
    parser.add_argument("--force-deepseek", action="store_true", help="skip quota detection")
    parser.add_argument("--force-openai", action="store_true", help="never use the fallback")
    parser.add_argument(
        "--threshold",
        type=float,
        default=float(os.environ.get("CODEX_FALLBACK_THRESHOLD", "100")),
        help="fallback at this used percentage (default: 100)",
    )
    options, codex_args = parser.parse_known_args(argv)
    if options.setup_key:
        save_key()
        return 0
    if options.force_deepseek:
        return run_deepseek(codex_args, "DeepSeek was explicitly selected")
    try:
        limits = read_limits()
        if options.status:
            print(format_status(limits))
            return 0
        exhausted = quota_exhausted(limits, options.threshold)
    except QuotaReadError as exc:
        if options.status:
            print(f"Unable to read Codex quota: {exc}", file=sys.stderr)
            return 2
        print(f"Quota check failed ({exc}); using Codex.", file=sys.stderr)
        exhausted = False
    if exhausted and not options.force_openai:
        return run_deepseek(codex_args)
    os.execv(CODEX, [CODEX, *codex_args])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
