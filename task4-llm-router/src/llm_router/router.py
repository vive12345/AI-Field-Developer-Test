"""Primary/secondary model routing with failover.

    primary ── 200 ───────────────────────────────▶ return
       │
       ├─ 429 / timeout (3000 ms) / unreachable / 5xx
       ▼
    secondary ── 200 ─────────────────────────────▶ return (fallback_reason set)
       └─ fails ──▶ 502 upstream_unavailable / 504 upstream_timeout

Timeout handling: `asyncio.timeout` puts a single deadline on the *whole*
primary call (connect + send + read body). A provider that trickles bytes
slowly cannot stretch past it, unlike per-operation socket timeouts. When the
deadline hits, the in-flight call is cancelled (its connection is closed), so
a late primary response can never race with the secondary's.

A 4xx (other than 429) means the request itself is bad; another provider
would reject it too, so it is reported without failover.
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

from llm_router import errors

logger = logging.getLogger(__name__)


class FailoverReason(str, Enum):
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    SERVER_ERROR = "server_error"


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    model: str
    timeout_seconds: float


@dataclass(frozen=True)
class RouteResult:
    provider: str
    completion: dict[str, Any]
    fallback_reason: FailoverReason | None = None


class _ProviderFailed(Exception):
    """Internal signal: this provider failed in a way that justifies trying the next one."""

    def __init__(self, reason: FailoverReason):
        super().__init__(reason.value)
        self.reason = reason


class ModelRouter:
    def __init__(self, http_client: httpx.AsyncClient, primary: Provider, secondary: Provider):
        self._http = http_client
        self._primary = primary
        self._secondary = secondary

    async def complete(self, body: dict[str, Any], request_id: str) -> RouteResult:
        try:
            completion = await self._call(self._primary, body)
            return RouteResult(self._primary.name, completion)
        except _ProviderFailed as failure:
            reason = failure.reason
            logger.warning("[%s] Primary failed (%s); failing over to secondary", request_id, reason.value)

        try:
            completion = await self._call(self._secondary, body)
            return RouteResult(self._secondary.name, completion, fallback_reason=reason)
        except _ProviderFailed as failure:
            logger.error("[%s] Secondary failed too (%s)", request_id, failure.reason.value)
            if FailoverReason.TIMEOUT in (reason, failure.reason):
                raise errors.upstream_timeout() from None
            raise errors.upstream_unavailable() from None

    async def _call(self, provider: Provider, body: dict[str, Any]) -> dict[str, Any]:
        payload = {**body, "model": provider.model}  # each provider serves its own model
        try:
            async with asyncio.timeout(provider.timeout_seconds):
                response = await self._http.post(provider.url, json=payload)
        except (TimeoutError, httpx.TimeoutException):
            raise _ProviderFailed(FailoverReason.TIMEOUT) from None
        except httpx.HTTPError:
            raise _ProviderFailed(FailoverReason.UNAVAILABLE) from None

        if response.status_code == 429:
            raise _ProviderFailed(FailoverReason.RATE_LIMITED)
        if response.status_code >= 500:
            # The body may contain stack traces: log only its size, never forward it.
            logger.warning("%s returned HTTP %s (%d-byte body withheld)", provider.name, response.status_code, len(response.content))
            raise _ProviderFailed(FailoverReason.SERVER_ERROR)
        if response.status_code != 200:
            raise errors.upstream_rejected()

        try:
            return response.json()
        except ValueError:
            raise _ProviderFailed(FailoverReason.SERVER_ERROR) from None
