"""从 OpenAI 兼容响应提取逐次调用用量；不保留原始回复内容。"""

import json

from src.agent.usage_pricing import TokenUsage


MAX_USAGE_BUFFER = 1024 * 1024


def usage_from_payload(payload: object) -> TokenUsage | None:
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if usage is None and isinstance(payload.get("response"), dict):
        usage = payload["response"].get("usage")
    if not isinstance(usage, dict):
        return None
    # Chat Completions 和 Responses 使用不同字段；缺失值不得推算为零。
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    details = usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
    cached_tokens = details.get("cached_tokens") if isinstance(details, dict) else None
    try:
        return TokenUsage(input_tokens, output_tokens, cached_tokens)
    except ValueError:
        return None


class ProviderUsageParser:
    """按字节块增量读取 SSE；普通 JSON 仅缓冲到上限，超限保持未知。"""

    def __init__(self):
        self._buffer = bytearray()
        self._sse = False
        self.usage: TokenUsage | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        if len(self._buffer) > MAX_USAGE_BUFFER:
            self._buffer.clear()
            return
        if self._buffer.startswith(b"data:") or b"\ndata:" in self._buffer:
            self._sse = True
        if not self._sse:
            return
        while b"\n" in self._buffer:
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            if not line.startswith(b"data:"):
                continue
            try:
                candidate = usage_from_payload(json.loads(line[5:].strip()))
            except (ValueError, UnicodeDecodeError):
                continue
            if candidate is not None:
                self.usage = candidate

    def finish(self) -> TokenUsage | None:
        if self._sse:
            self.feed(b"\n")
            return self.usage
        try:
            return usage_from_payload(json.loads(self._buffer))
        except (ValueError, UnicodeDecodeError):
            return None
