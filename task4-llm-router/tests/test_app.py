"""End-to-end tests: router app + real mock provider apps, all in memory."""

import asyncio

import httpx
import pytest

from llm_router.app import create_app
from llm_router.config import Settings
from mock_providers.app import create_app as create_provider

pytestmark = pytest.mark.anyio

ACME = {"Authorization": "Bearer tenant-acme-key"}
TINY = {"Authorization": "Bearer tenant-tiny-key"}  # 2,000 tokens per minute
LEAK_MARKERS = ("Traceback", "10.0.3.17", "sk-internal", "inference.py", "CUDA")


def chat(content: str = "Summarize the report", **extra) -> dict:
    return {"messages": [{"role": "user", "content": content}], **extra}


class HostRouter(httpx.AsyncBaseTransport):
    """Send requests to an in-memory app chosen by URL host."""

    def __init__(self, apps: dict):
        self._transports = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transports[request.url.host].handle_async_request(request)


def build_gateway(db_path: str, transport: httpx.AsyncBaseTransport, primary_timeout: float = 0.3):
    settings = Settings(
        primary_url="http://primary/v1/chat/completions",
        secondary_url="http://secondary/v1/chat/completions",
        primary_timeout_seconds=primary_timeout,
        secondary_timeout_seconds=primary_timeout,
        db_path=db_path,
    )
    app = create_app(settings, http_client=httpx.AsyncClient(transport=transport))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://gateway"
    )
    return app, client


@pytest.fixture
def providers():
    return {"primary": create_provider("primary", slow_seconds=2), "secondary": create_provider("secondary", slow_seconds=2)}


@pytest.fixture
async def gateway(db_path, providers):
    app, client = build_gateway(db_path, HostRouter(providers))
    async with client:
        yield app, client


def assert_standard_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    assert not any(marker in response.text for marker in LEAK_MARKERS)


# --- routing --------------------------------------------------------------------


async def test_primary_serves_when_healthy(gateway):
    _, client = gateway
    response = await client.post("/v1/chat/completions", json=chat(), headers=ACME)

    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "primary"
    assert "x-gateway-fallback-reason" not in response.headers
    assert response.json()["model"] == "primary-model"
    assert response.headers["x-request-id"].startswith("req_")


@pytest.mark.parametrize(("mode", "reason"), [("rate_limited", "rate_limited"), ("slow", "timeout"), ("error", "server_error")])
async def test_primary_failures_fall_back_to_secondary(gateway, providers, mode, reason):
    _, client = gateway
    providers["primary"].state.mode = mode

    response = await client.post("/v1/chat/completions", json=chat(), headers=ACME)

    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "secondary"
    assert response.headers["x-gateway-fallback-reason"] == reason
    assert response.json()["model"] == "secondary-model"


@pytest.mark.parametrize(
    ("primary", "secondary", "status", "code"),
    [("error", "error", 502, "upstream_unavailable"), ("slow", "slow", 504, "upstream_timeout")],
)
async def test_total_outage_returns_sanitized_error_and_refunds_tokens(gateway, providers, primary, secondary, status, code):
    app, client = gateway
    providers["primary"].state.mode = primary
    providers["secondary"].state.mode = secondary

    response = await client.post("/v1/chat/completions", json=chat(), headers=ACME)

    assert_standard_error(response, status, code)
    assert app.state.limiter.usage("acme") == 0  # failed request consumed nothing


# --- token accounting and rate limiting -----------------------------------------


async def test_reservation_is_settled_to_reported_usage(gateway):
    app, client = gateway
    response = await client.post("/v1/chat/completions", json=chat(max_tokens=1000), headers=ACME)

    reported = response.json()["usage"]["total_tokens"]
    assert app.state.limiter.usage("acme") == reported
    assert response.headers["x-ratelimit-remaining"] == str(50_000 - reported)


async def test_rate_limit_returns_429_with_retry_after(db_path):
    async def heavy_provider(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [], "usage": {"total_tokens": 1_500}})

    _, client = build_gateway(db_path, httpx.MockTransport(heavy_provider))
    async with client:
        first = await client.post("/v1/chat/completions", json=chat(max_tokens=100), headers=TINY)
        second = await client.post("/v1/chat/completions", json=chat(max_tokens=600), headers=TINY)

    assert first.status_code == 200
    assert_standard_error(second, 429, "rate_limit_exceeded")
    assert 1 <= int(second.headers["retry-after"]) <= 60
    assert second.headers["x-ratelimit-remaining"] == "0"


async def test_concurrent_requests_respect_the_budget(db_path):
    async def slow_provider(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)  # keep reservations in flight at the same time
        return httpx.Response(200, json={"choices": []})  # no usage -> estimate is kept

    _, client = build_gateway(db_path, httpx.MockTransport(slow_provider))
    async with client:
        # Each request reserves 4 + ceil(20/4) + 600 = 609 tokens; 2,000 // 609 = 3 fit.
        responses = await asyncio.gather(
            *[client.post("/v1/chat/completions", json=chat("x" * 20, max_tokens=600), headers=TINY) for _ in range(10)]
        )

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200] * 3 + [429] * 7


async def test_request_larger_than_budget_is_rejected_up_front(gateway):
    _, client = gateway
    response = await client.post("/v1/chat/completions", json=chat(max_tokens=5_000), headers=TINY)

    assert_standard_error(response, 400, "request_too_large")


# --- standardized errors ----------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic tenant-acme-key"}])
async def test_invalid_api_key(gateway, headers):
    _, client = gateway
    response = await client.post("/v1/chat/completions", json=chat(), headers=headers)

    assert_standard_error(response, 401, "invalid_api_key")
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "body",
    [b"{not json", b"[]", b'{"messages": []}', b'{"messages": [{"content": 5}]}', b'{"messages": [{"content": "hi"}], "stream": true}'],
)
async def test_invalid_requests(gateway, body):
    _, client = gateway
    response = await client.post("/v1/chat/completions", content=body, headers=ACME)

    assert_standard_error(response, 400, "invalid_request")


async def test_unknown_routes_use_the_standard_payload(gateway):
    _, client = gateway

    assert_standard_error(await client.get("/does-not-exist"), 404, "not_found")
    assert_standard_error(await client.get("/v1/chat/completions"), 405, "method_not_allowed")


async def test_unexpected_exceptions_are_sanitized(gateway):
    app, client = gateway

    async def explode(body, request_id):
        raise RuntimeError("db password=hunter2 at /srv/internal/router.py")

    app.state.router.complete = explode
    response = await client.post("/v1/chat/completions", json=chat(), headers=ACME)

    assert_standard_error(response, 500, "internal_error")
    assert "hunter2" not in response.text
    assert app.state.limiter.usage("acme") == 0  # reservation refunded
