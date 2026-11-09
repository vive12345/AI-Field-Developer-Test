"""Gateway settings, read from environment variables with sensible defaults."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Any OpenAI-compatible /v1/chat/completions endpoint.
    upstream_url: str = "http://127.0.0.1:9100/v1/chat/completions"
    # Provider credential stays at the gateway; clients never see it.
    upstream_api_key: str | None = None
    upstream_timeout_seconds: float = 30.0
    host: str = "127.0.0.1"
    port: int = 8100

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            upstream_url=os.getenv("LLM_UPSTREAM_URL", cls.upstream_url),
            upstream_api_key=os.getenv("LLM_UPSTREAM_API_KEY"),
            upstream_timeout_seconds=float(os.getenv("LLM_UPSTREAM_TIMEOUT", cls.upstream_timeout_seconds)),
            host=os.getenv("LLM_GATEWAY_HOST", cls.host),
            port=int(os.getenv("LLM_GATEWAY_PORT", cls.port)),
        )
