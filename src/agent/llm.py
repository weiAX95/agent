"""Create the production chat model only when the API starts."""

from functools import lru_cache
from contextvars import ContextVar

from src.agent.model_gateway import ModelGatewayConfig, build_model


active_model_profile: ContextVar[str | None] = ContextVar("active_model_profile", default=None)


@lru_cache(maxsize=10)
def _cached_llm(profile: str | None):
    return build_model(ModelGatewayConfig.from_env(profile))


def get_llm(profile: str | None = None):
    return _cached_llm(profile or active_model_profile.get())


get_llm.cache_clear = _cached_llm.cache_clear
