"""Gateway settings, read from environment variables with sensible defaults."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    downstream_url: str = "http://127.0.0.1:9000/mcp"
    downstream_timeout_seconds: float = 10.0
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            downstream_url=os.getenv("GATEWAY_DOWNSTREAM_URL", cls.downstream_url),
            downstream_timeout_seconds=float(
                os.getenv("GATEWAY_DOWNSTREAM_TIMEOUT", cls.downstream_timeout_seconds)
            ),
            host=os.getenv("GATEWAY_HOST", cls.host),
            port=int(os.getenv("GATEWAY_PORT", cls.port)),
        )
