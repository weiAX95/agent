"""Model-provider boundary for the production chat application."""

import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import openai
from langchain.chat_models import init_chat_model


MODEL_ERROR_MESSAGES = {
    "MODEL_TIMEOUT": "模型响应超时，请使用新的 request_id 重试。",
    "MODEL_RATE_LIMITED": "模型服务繁忙，请稍后使用新的 request_id 重试。",
    "MODEL_UNAVAILABLE": "模型服务暂不可用，请稍后使用新的 request_id 重试。",
    "MODEL_AUTH_FAILED": "模型服务配置不可用，请联系管理员。",
}


@dataclass(frozen=True)
class ModelGatewayConfig:
    profile: str
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float
    max_retries: int

    @classmethod
    def from_env(cls, profile: str | None = None) -> "ModelGatewayConfig":
        profiles = _profile_definitions()
        selected = profile or default_profile_name(profiles)
        if selected not in profiles:
            raise ValueError("未知的模型档位")
        definition = profiles[selected]
        model = definition["model"].strip()
        base_url = definition["base_url"].strip()
        api_key = os.getenv(definition["api_key_env"], "").strip()
        if not model or not api_key:
            raise ValueError(f"模型档位 {selected} 缺少模型名或 {definition['api_key_env']}")
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError(f"模型档位 {selected} 的 base_url 必须是无凭证的 HTTP(S) 地址")
        try:
            timeout = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
            retries = int(os.getenv("MODEL_MAX_RETRIES", "0"))
        except ValueError as exc:
            raise ValueError("MODEL_TIMEOUT_SECONDS 或 MODEL_MAX_RETRIES 格式无效") from exc
        if not 5 <= timeout <= 300 or not 0 <= retries <= 2:
            raise ValueError("模型超时须为 5–300 秒，重试次数须为 0–2")
        return cls(selected, model, base_url.rstrip("/"), api_key, timeout, retries)


def _profile_definitions() -> dict[str, dict[str, str]]:
    legacy = {"model": os.getenv("OPENAI_MODEL", ""),
              "base_url": os.getenv("OPENAI_BASE_URL", ""),
              "api_key_env": "OPENAI_API_KEY"}
    raw = os.getenv("MODEL_UPSTREAMS_JSON", "").strip()
    if not raw:
        return {"default": legacy}
    try:
        profiles = json.loads(raw)
    except ValueError as exc:
        raise ValueError("MODEL_UPSTREAMS_JSON 必须是 JSON 对象") from exc
    if not isinstance(profiles, dict):
        raise ValueError("MODEL_UPSTREAMS_JSON 必须是 JSON 对象")
    if all(legacy[key].strip() for key in ("model", "base_url")) and "default" not in profiles:
        profiles = {"default": legacy, **profiles}
    if not 1 <= len(profiles) <= 10:
        raise ValueError("模型档位总数必须为 1–10 个")
    for name, entry in profiles.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name)
                or not isinstance(entry, dict)
                or any(not isinstance(entry.get(key), str) or not entry[key].strip()
                       for key in ("model", "base_url", "api_key_env"))
                or not re.fullmatch(r"[A-Z][A-Z0-9_]*", entry["api_key_env"])):
            raise ValueError("模型档位名称或配置无效")
    return profiles


def available_profiles() -> list[dict[str, str]]:
    """Public metadata only; API keys and their environment variable names stay private."""
    profiles = _profile_definitions()
    return [{"id": name, "model": entry["model"]} for name, entry in profiles.items()]


def default_profile_name(profiles: dict | None = None) -> str:
    definitions = profiles if profiles is not None else _profile_definitions()
    selected = os.getenv("MODEL_DEFAULT_PROFILE", "").strip() or next(iter(definitions))
    if selected not in definitions:
        raise ValueError("默认模型档位不存在")
    return selected


def build_model(config: ModelGatewayConfig):
    """Retry only an individual upstream model call, never an entire agent run."""
    return init_chat_model(
        model=config.model,
        model_provider="openai",
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.timeout_seconds,
        max_retries=config.max_retries,
    )


def classify_model_error(exc: Exception) -> tuple[str, str] | None:
    """Return a stable client code without exposing upstream error text."""
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(5):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        code = None
        if isinstance(current, openai.APITimeoutError):
            code = "MODEL_TIMEOUT"
        elif isinstance(current, openai.RateLimitError):
            code = "MODEL_RATE_LIMITED"
        elif isinstance(current, openai.APIConnectionError):
            code = "MODEL_UNAVAILABLE"
        elif isinstance(current, openai.APIStatusError) and current.status_code in {401, 403}:
            code = "MODEL_AUTH_FAILED"
        elif isinstance(current, openai.APIStatusError) and current.status_code >= 500:
            code = "MODEL_UNAVAILABLE"
        elif type(current).__name__ == "OpenAITimeoutError":
            code = "MODEL_TIMEOUT"
        elif type(current).__name__ == "OpenAIRateLimitError":
            code = "MODEL_RATE_LIMITED"
        elif type(current).__name__ == "OpenAIConnectionError":
            code = "MODEL_UNAVAILABLE"
        elif type(current).__name__ in {"OpenAIAuthenticationError", "OpenAIPermissionDeniedError"}:
            code = "MODEL_AUTH_FAILED"
        if code:
            return code, MODEL_ERROR_MESSAGES[code]
        current = current.__cause__ or current.__context__
    return None
