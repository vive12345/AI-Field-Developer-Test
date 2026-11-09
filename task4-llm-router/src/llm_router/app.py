"""Gateway HTTP application: POST /v1/chat/completions.

Request pipeline:
    1. authenticate tenant (API key)           -> 401 invalid_api_key
    2. validate body, estimate token cost      -> 400 invalid_request / request_too_large
    3. reserve tokens in the sliding window     -> 429 rate_limit_exceeded (+ Retry-After)
    4. route: primary, failover to secondary    -> 502 / 504 when both fail
    5. settle: adjust reservation to real usage, or refund it if no completion

Every response carries X-Request-Id; every error uses the standard payload.
"""

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from llm_router import __version__, errors
from llm_router.config import Settings
from llm_router.errors import GatewayError, error_payload
from llm_router.rate_limiter import SlidingWindowRateLimiter
from llm_router.router import ModelRouter, Provider, RouteResult
from llm_router.tenants import TenantStore
from llm_router.tokens import estimate_request_tokens, validate_request

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
    tenant_store: TenantStore | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    tenants = tenant_store or TenantStore.from_file()
    limiter = SlidingWindowRateLimiter(settings.db_path, settings.window_seconds)
    primary = Provider("primary", settings.primary_url, settings.primary_model, settings.primary_timeout_seconds)
    secondary = Provider("secondary", settings.secondary_url, settings.secondary_model, settings.secondary_timeout_seconds)

    @asynccontextmanager
    async def own_http_client(app: FastAPI):
        # No client-level timeout: the router enforces a per-provider deadline itself.
        async with httpx.AsyncClient(timeout=None) as client:
            app.state.router = ModelRouter(client, primary, secondary)
            yield

    app = FastAPI(
        title="LLM Router",
        version=__version__,
        lifespan=own_http_client if http_client is None else None,
    )
    app.state.limiter = limiter
    if http_client is not None:
        app.state.router = ModelRouter(http_client, primary, secondary)

    _install_error_handlers(app)

    @app.middleware("http")
    async def assign_request_id(request: Request, call_next):
        request.state.request_id = f"req_{uuid.uuid4().hex[:16]}"
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:
        request_id = request.state.request_id
        tenant = tenants.authenticate(request.headers.get("authorization"))
        body = validate_request(await _read_json(request))

        estimated = estimate_request_tokens(body, settings.default_max_tokens)
        if estimated > tenant.tokens_per_minute:
            raise errors.request_too_large(tenant.tokens_per_minute)

        decision = await asyncio.to_thread(limiter.try_acquire, tenant.tenant_id, estimated, tenant.tokens_per_minute)
        if not decision.allowed:
            logger.info("[%s] Rate limited tenant=%s (needs %d tokens)", request_id, tenant.tenant_id, estimated)
            raise errors.rate_limited(decision.limit, decision.retry_after_seconds)

        try:
            result = await request.app.state.router.complete(body, request_id)
        except BaseException:
            # No completion was produced (error or client disconnect): refund the reservation.
            # Synchronous on purpose, so it still runs while the task is being cancelled.
            limiter.release(decision.reservation_id)
            raise

        remaining = await _settle_usage(limiter, decision.reservation_id, estimated, decision.remaining, result)
        logger.info(
            "[%s] tenant=%s provider=%s fallback=%s",
            request_id,
            tenant.tenant_id,
            result.provider,
            result.fallback_reason.value if result.fallback_reason else "none",
        )
        return JSONResponse(result.completion, headers=_routing_headers(result, decision.limit, remaining))

    return app


async def _read_json(request: Request):
    try:
        return json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        raise errors.invalid_request("Request body must be valid JSON") from None


async def _settle_usage(limiter, reservation_id: int, estimated: int, remaining: int, result: RouteResult) -> int:
    """Replace the estimate with the provider-reported usage, when available."""
    actual = result.completion.get("usage", {}).get("total_tokens")
    if not isinstance(actual, int) or isinstance(actual, bool):
        return remaining
    await asyncio.to_thread(limiter.adjust, reservation_id, actual)
    return max(0, remaining + estimated - actual)


def _routing_headers(result: RouteResult, limit: int, remaining: int) -> dict[str, str]:
    headers = {
        "X-Gateway-Provider": result.provider,
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
    }
    if result.fallback_reason:
        headers["X-Gateway-Fallback-Reason"] = result.fallback_reason.value
    return headers


def _install_error_handlers(app: FastAPI) -> None:
    def respond(request: Request, error: GatewayError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "req_unknown")
        headers = {**error.headers, "X-Request-Id": request_id}
        return JSONResponse(error_payload(error.code, error.message, request_id), error.status, headers=headers)

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, error: GatewayError) -> JSONResponse:
        return respond(request, error)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        mapped = {404: errors.not_found(), 405: errors.method_not_allowed()}.get(error.status_code)
        return respond(request, mapped or errors.internal_error())

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, error: Exception) -> JSONResponse:
        # Full details (stack trace) go to the log only, correlated by request_id.
        logger.exception("[%s] Unhandled error", getattr(request.state, "request_id", "req_unknown"))
        return respond(request, errors.internal_error())
