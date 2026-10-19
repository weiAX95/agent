from tools.codex_deepseek.codex_auto import format_status, quota_exhausted
from tools.codex_deepseek.gateway import (
    app,
    build_upstream_payload,
    responses_input_to_messages,
    responses_tools_to_chat,
)
from fastapi.testclient import TestClient


def limits(primary=10, secondary=20, reached=None):
    return {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": {"usedPercent": primary, "windowDurationMins": 300, "resetsAt": 1},
                "secondary": {
                    "usedPercent": secondary,
                    "windowDurationMins": 10080,
                    "resetsAt": 2,
                },
                "rateLimitReachedType": reached,
            }
        }
    }


def test_quota_switches_at_exhaustion_or_server_classification():
    assert not quota_exhausted(limits(99, 99))
    assert quota_exhausted(limits(100, 20))
    assert quota_exhausted(limits(20, 100))
    assert quota_exhausted(limits(20, 20, "weekly"))


def test_quota_supports_early_threshold():
    assert quota_exhausted(limits(95, 20), threshold=95)
    assert "primary: used 95%" in format_status(limits(95, 20))


def test_responses_history_becomes_chat_messages():
    payload = {
        "instructions": "Be useful",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "shell",
                "arguments": '{"command":"pwd"}',
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "/tmp"},
        ],
    }
    messages = responses_input_to_messages(payload)
    assert messages[0] == {"role": "system", "content": "Be useful"}
    assert messages[1] == {"role": "user", "content": "hi"}
    assert messages[2]["tool_calls"][0]["function"]["name"] == "shell"
    assert messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": "/tmp"}


def test_tools_and_model_are_translated(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-reasoner")
    tools = responses_tools_to_chat(
        [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]
    )
    assert tools[0]["function"]["name"] == "read_file"
    upstream = build_upstream_payload(
        {"input": "hello", "tools": [{"type": "function", "name": "read_file"}]},
        stream=True,
    )
    assert upstream["model"] == "deepseek-reasoner"
    assert upstream["stream"] is True
    assert upstream["messages"] == [{"role": "user", "content": "hello"}]


def test_gateway_emits_responses_sse(monkeypatch):
    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"hello"}}]}'
            yield 'data: {"choices":[{"delta":{"content":" world"}}],"usage":{"prompt_tokens":2,"completion_tokens":2,"total_tokens":4}}'
            yield "data: [DONE]"

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return FakeStream()

    monkeypatch.setenv("CODEX_GATEWAY_API_KEY", "local-test-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-token")
    monkeypatch.setattr("tools.codex_deepseek.gateway.httpx.AsyncClient", FakeClient)
    client = TestClient(app)
    response = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer local-test-token"},
        json={"model": "deepseek-chat", "input": "hello", "stream": True},
    )
    assert response.status_code == 200
    assert "response.output_text.delta" in response.text
    assert "hello world" in response.text
    assert "response.completed" in response.text
