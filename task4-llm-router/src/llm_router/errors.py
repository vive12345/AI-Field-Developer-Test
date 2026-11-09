"""Standardized gateway errors.

Every error leaving the gateway has the same shape:

    {"error": {"code": "<stable_code>", "message": "<safe text>", "request_id": "req_..."}}

Messages are written by the gateway, never copied from an upstream provider,
so raw upstream bodies, stack traces and internal URLs cannot leak. Details
for debugging go to the logs, correlated by request_id.
"""

import math
from typing import Any


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def error_payload(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def invalid_api_key() -> GatewayError:
    return GatewayError(401, "invalid_api_key", "Missing or invalid API key", {"WWW-Authenticate": "Bearer"})


def invalid_request(message: str) -> GatewayError:
    return GatewayError(400, "invalid_request", message)


def request_too_large(limit: int) -> GatewayError:
    return GatewayError(400, "request_too_large", f"Request needs more tokens than the per-minute limit of {limit}")


def rate_limited(limit: int, retry_after_seconds: float) -> GatewayError:
    retry_after = max(1, math.ceil(retry_after_seconds))
    return GatewayError(
        429,
        "rate_limit_exceeded",
        f"Token rate limit of {limit} tokens per minute exceeded. Retry after {retry_after}s.",
        {"Retry-After": str(retry_after), "X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0"},
    )


def upstream_rejected() -> GatewayError:
    return GatewayError(400, "invalid_request", "The model provider rejected the request")


def upstream_unavailable() -> GatewayError:
    return GatewayError(502, "upstream_unavailable", "All model providers are currently unavailable")


def upstream_timeout() -> GatewayError:
    return GatewayError(504, "upstream_timeout", "The model providers did not respond in time")


def not_found() -> GatewayError:
    return GatewayError(404, "not_found", "Resource not found")


def method_not_allowed() -> GatewayError:
    return GatewayError(405, "method_not_allowed", "Method not allowed")


def internal_error() -> GatewayError:
    return GatewayError(500, "internal_error", "An internal error occurred")
