"""A small Responses API adapter for DeepSeek's Chat Completions API.

The adapter intentionally binds to localhost when launched by ``codex-auto``.
It translates the subset of the Responses protocol used by local Codex turns,
including streamed text and function calls.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse


app = FastAPI(title="Codex DeepSeek Responses adapter", docs_url=None, redoc_url=None)
logger = logging.getLogger("codex_deepseek.gateway")


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for part in value:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                chunks.append(str(part.get("text") or part.get("output") or ""))
        return "\n".join(chunk for chunk in chunks if chunk)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def responses_input_to_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert replayed Responses input items to Chat Completions messages."""
    messages: list[dict[str, Any]] = []
    instructions = payload.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": _text(instructions)})

    raw_input = payload.get("input", [])
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
        return messages

    for item in raw_input:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            messages.append({"role": role, "content": _text(item.get("content"))})
        elif kind in {"function_call", "custom_tool_call"}:
            arguments = item.get("arguments")
            if arguments is None:
                arguments = json.dumps({"input": item.get("input", "")}, ensure_ascii=False)
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}",
                            "type": "function",
                            "function": {
                                "name": item.get("name", "tool"),
                                "arguments": arguments,
                            },
                        }
                    ],
                }
            )
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or item.get("id"),
                    "content": _text(item.get("output")),
                }
            )
    return messages


def responses_tools_to_chat(tools: Any) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return converted
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        parameters = tool.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
                "additionalProperties": False,
            }
        function: dict[str, Any] = {"name": tool["name"], "parameters": parameters}
        if tool.get("description"):
            function["description"] = tool["description"]
        converted.append({"type": "function", "function": function})
    return converted


def build_upstream_payload(payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
    upstream: dict[str, Any] = {
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "messages": responses_input_to_messages(payload),
        "stream": stream,
    }
    tools = responses_tools_to_chat(payload.get("tools"))
    if tools:
        upstream["tools"] = tools
        choice = payload.get("tool_choice")
        if choice in {"auto", "none", "required"}:
            upstream["tool_choice"] = choice
    if payload.get("max_output_tokens"):
        upstream["max_tokens"] = payload["max_output_tokens"]
    if payload.get("temperature") is not None:
        upstream["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        upstream["top_p"] = payload["top_p"]
    return upstream


def _response_shell(payload: dict[str, Any], response_id: str, status: str) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": payload.get("model", os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")),
        "output": [],
        "error": None,
        "incomplete_details": None,
        "parallel_tool_calls": payload.get("parallel_tool_calls", True),
        "previous_response_id": payload.get("previous_response_id"),
        "usage": None,
    }


def _sse(event: dict[str, Any]) -> bytes:
    event_type = event["type"]
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {data}\n\n".encode()


def _gateway_token_valid(authorization: str | None) -> bool:
    expected = os.environ.get("CODEX_GATEWAY_API_KEY", "")
    if not expected or not authorization or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization[7:], expected)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


async def _stream_response(payload: dict[str, Any]) -> AsyncIterator[bytes]:
    response_id = f"resp_{uuid.uuid4().hex}"
    sequence = 0
    shell = _response_shell(payload, response_id, "in_progress")

    def event(event_type: str, **values: Any) -> bytes:
        nonlocal sequence
        body = {"type": event_type, "sequence_number": sequence, **values}
        sequence += 1
        return _sse(body)

    yield event("response.created", response=shell)
    upstream_payload = build_upstream_payload(payload, stream=True)
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    outputs: list[dict[str, Any]] = []
    message_item: dict[str, Any] | None = None
    tool_items: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] | None = None

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=20.0)) as client:
            async with client.stream(
                "POST", f"{base_url}/chat/completions", headers=headers, json=upstream_payload
            ) as upstream:
                if upstream.status_code >= 400:
                    detail = (await upstream.aread()).decode(errors="replace")[:2000]
                    logger.error("DeepSeek returned HTTP %s: %s", upstream.status_code, detail)
                    raise HTTPException(upstream.status_code, f"DeepSeek upstream error: {detail}")
                async for line in upstream.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    chunk = json.loads(raw)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content") or ""
                    if content:
                        if message_item is None:
                            message_item = {
                                "id": f"msg_{uuid.uuid4().hex}",
                                "type": "message",
                                "status": "in_progress",
                                "role": "assistant",
                                "content": [],
                            }
                            outputs.append(message_item)
                            output_index = len(outputs) - 1
                            yield event(
                                "response.output_item.added",
                                output_index=output_index,
                                item=message_item,
                            )
                            part = {"type": "output_text", "text": "", "annotations": []}
                            message_item["content"].append(part)
                            yield event(
                                "response.content_part.added",
                                item_id=message_item["id"],
                                output_index=output_index,
                                content_index=0,
                                part=part,
                            )
                        output_index = outputs.index(message_item)
                        message_item["content"][0]["text"] += content
                        yield event(
                            "response.output_text.delta",
                            item_id=message_item["id"],
                            output_index=output_index,
                            content_index=0,
                            delta=content,
                            logprobs=[],
                        )
                    for tool_delta in delta.get("tool_calls") or []:
                        tool_index = int(tool_delta.get("index", 0))
                        function = tool_delta.get("function") or {}
                        if tool_index not in tool_items:
                            item = {
                                "id": f"fc_{uuid.uuid4().hex}",
                                "type": "function_call",
                                "status": "in_progress",
                                "call_id": tool_delta.get("id") or f"call_{uuid.uuid4().hex}",
                                "name": function.get("name") or "tool",
                                "arguments": "",
                            }
                            tool_items[tool_index] = item
                            outputs.append(item)
                            yield event(
                                "response.output_item.added",
                                output_index=len(outputs) - 1,
                                item=item,
                            )
                        item = tool_items[tool_index]
                        if tool_delta.get("id"):
                            item["call_id"] = tool_delta["id"]
                        if function.get("name"):
                            item["name"] = function["name"]
                        arguments = function.get("arguments") or ""
                        if arguments:
                            item["arguments"] += arguments
                            yield event(
                                "response.function_call_arguments.delta",
                                item_id=item["id"],
                                output_index=outputs.index(item),
                                delta=arguments,
                            )
    except HTTPException as exc:
        error = {"type": "server_error", "code": str(exc.status_code), "message": str(exc.detail)}
        yield event("error", error=error)
        return
    except Exception as exc:  # pragma: no cover - exercised by live connectivity tests
        error = {"type": "server_error", "code": "gateway_error", "message": str(exc)}
        yield event("error", error=error)
        return

    if message_item is None and not tool_items:
        message_item = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "", "annotations": []}],
        }
        outputs.append(message_item)
        yield event("response.output_item.added", output_index=0, item=message_item)
        yield event(
            "response.content_part.added",
            item_id=message_item["id"],
            output_index=0,
            content_index=0,
            part=message_item["content"][0],
        )

    for output_index, item in enumerate(outputs):
        if item["type"] == "message":
            text = item["content"][0]["text"]
            yield event(
                "response.output_text.done",
                item_id=item["id"],
                output_index=output_index,
                content_index=0,
                text=text,
                logprobs=[],
            )
            item["content"][0]["text"] = text
            yield event(
                "response.content_part.done",
                item_id=item["id"],
                output_index=output_index,
                content_index=0,
                part=item["content"][0],
            )
        else:
            yield event(
                "response.function_call_arguments.done",
                item_id=item["id"],
                output_index=output_index,
                arguments=item["arguments"],
            )
        item["status"] = "completed"
        yield event("response.output_item.done", output_index=output_index, item=item)

    completed = _response_shell(payload, response_id, "completed")
    completed["output"] = outputs
    if usage:
        completed["usage"] = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
    yield event("response.completed", response=completed)


@app.post("/v1/responses", response_model=None)
async def responses(
    request: Request, authorization: str | None = Header(default=None)
) -> StreamingResponse:
    if not _gateway_token_valid(authorization):
        raise HTTPException(401, "Invalid gateway credential")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise HTTPException(503, "DEEPSEEK_API_KEY is not configured")
    payload = await request.json()
    if not payload.get("stream", True):
        raise HTTPException(400, "This adapter requires stream=true")
    return StreamingResponse(_stream_response(payload), media_type="text/event-stream")
