"""Router settings, read from environment variables with sensible defaults."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    primary_url: str = "http://127.0.0.1:9201/v1/chat/completions"
    primary_model: str = "primary-model"
    primary_timeout_seconds: float = 3.0  # deadline for the whole primary call

    secondary_url: str = "http://127.0.0.1:9202/v1/chat/completions"
    secondary_model: str = "secondary-model"
    secondary_timeout_seconds: float = 10.0

    db_path: str = "router.db"  # on-disk SQLite file holding rate-limit state
    window_seconds: float = 60.0
    default_max_tokens: int = 512  # reserved when a request does not set max_tokens

    host: str = "127.0.0.1"
    port: int = 8200

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.getenv
        return cls(
            primary_url=env("ROUTER_PRIMARY_URL", cls.primary_url),
            primary_model=env("ROUTER_PRIMARY_MODEL", cls.primary_model),
            primary_timeout_seconds=float(env("ROUTER_PRIMARY_TIMEOUT", cls.primary_timeout_seconds)),
            secondary_url=env("ROUTER_SECONDARY_URL", cls.secondary_url),
            secondary_model=env("ROUTER_SECONDARY_MODEL", cls.secondary_model),
            secondary_timeout_seconds=float(env("ROUTER_SECONDARY_TIMEOUT", cls.secondary_timeout_seconds)),
            db_path=env("ROUTER_DB_PATH", cls.db_path),
            window_seconds=float(env("ROUTER_WINDOW_SECONDS", cls.window_seconds)),
            default_max_tokens=int(env("ROUTER_DEFAULT_MAX_TOKENS", cls.default_max_tokens)),
            host=env("ROUTER_HOST", cls.host),
            port=int(env("ROUTER_PORT", cls.port)),
        )
