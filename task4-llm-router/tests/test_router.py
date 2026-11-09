"""Model router: failover rules, deadline enforcement and cancellation of the timed-out call."""

import asyncio
import time

import httpx
import pytest

from llm_router.errors import GatewayError
from llm_router.router import FailoverReason, ModelRouter, Provider

pytestmark = pytest.mark.anyio

TIMEOUT = 0.2
OK = {"choices": [], "usage": {"total_tokens": 10}}


class FakeProviders:
    """MockTransport handler whose behaviour is set per provider."""

    def __init__(self, primary: str = "ok", secondary: str = "ok", slow_seconds: float = 1.0):
        self.behaviour = {"primary": primary, "secondary": secondary}
        self.calls = {"primary": 0, "secondary": 0}
        self.slow_seconds = slow_seconds
        self.primary_cancelled = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        name = request.url.host
        self.calls[name] += 1
        behaviour = self.behaviour[name]

        if behaviour == "slow":
            try:
                await asyncio.sleep(self.slow_seconds)
            except asyncio.CancelledError:
                self.primary_cancelled |= name == "primary"
                raise
        if behaviour == "429":
            return httpx.Response(429)
        if behaviour == "500":
            return httpx.Response(500, text="Traceback: secret")
        if behaviour == "400":
            return httpx.Response(400)
        if behaviour == "down":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={**OK, "model": request.url.host})


def make_router(providers: FakeProviders) -> ModelRouter:
    http = httpx.AsyncClient(transport=httpx.MockTransport(providers))
    return ModelRouter(
        http,
        Provider("primary", "http://primary/v1", "p-model", TIMEOUT),
        Provider("secondary", "http://secondary/v1", "s-model", TIMEOUT),
    )


async def test_primary_success_does_not_touch_secondary():
    providers = FakeProviders()
    result = await make_router(providers).complete({"messages": []}, "req_1")

    assert result.provider == "primary" and result.fallback_reason is None
    assert providers.calls["secondary"] == 0


@pytest.mark.parametrize(
    ("primary", "reason"),
    [
        ("429", FailoverReason.RATE_LIMITED),
        ("slow", FailoverReason.TIMEOUT),
        ("500", FailoverReason.SERVER_ERROR),
        ("down", FailoverReason.UNAVAILABLE),
    ],
)
async def test_fails_over_to_secondary(primary, reason):
    providers = FakeProviders(primary=primary)
    result = await make_router(providers).complete({"messages": []}, "req_1")

    assert result.provider == "secondary"
    assert result.fallback_reason is reason
    assert result.completion["model"] == "secondary"


async def test_each_provider_receives_its_own_model_name():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.read()))
        return httpx.Response(429) if request.url.host == "primary" else httpx.Response(200, json=OK)

    router = ModelRouter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        Provider("primary", "http://primary/v1", "big-model", TIMEOUT),
        Provider("secondary", "http://secondary/v1", "backup-model", TIMEOUT),
    )
    await router.complete({"model": "client-choice", "messages": []}, "req_1")

    assert b'"model":"big-model"' in seen[0][1].replace(b" ", b"")
    assert b'"model":"backup-model"' in seen[1][1].replace(b" ", b"")


async def test_timeout_fails_over_at_the_deadline_and_cancels_the_primary():
    providers = FakeProviders(primary="slow", slow_seconds=5)

    started = time.perf_counter()
    result = await make_router(providers).complete({"messages": []}, "req_1")
    elapsed = time.perf_counter() - started

    assert result.fallback_reason is FailoverReason.TIMEOUT
    assert TIMEOUT <= elapsed < TIMEOUT + 0.15  # did not wait for the 5s primary
    assert providers.primary_cancelled  # the late primary call cannot race the secondary


async def test_primary_answering_just_before_the_deadline_is_used():
    providers = FakeProviders(primary="slow", slow_seconds=TIMEOUT / 2)
    result = await make_router(providers).complete({"messages": []}, "req_1")

    assert result.provider == "primary"
    assert providers.calls["secondary"] == 0


async def test_client_errors_are_not_retried_on_secondary():
    providers = FakeProviders(primary="400")

    with pytest.raises(GatewayError) as error:
        await make_router(providers).complete({"messages": []}, "req_1")

    assert error.value.status == 400
    assert providers.calls["secondary"] == 0


@pytest.mark.parametrize(
    ("primary", "secondary", "status", "code"),
    [
        ("429", "500", 502, "upstream_unavailable"),
        ("down", "down", 502, "upstream_unavailable"),
        ("slow", "slow", 504, "upstream_timeout"),
        ("429", "slow", 504, "upstream_timeout"),
    ],
)
async def test_both_providers_failing_raises_a_sanitized_gateway_error(primary, secondary, status, code):
    providers = FakeProviders(primary=primary, secondary=secondary)

    with pytest.raises(GatewayError) as error:
        await make_router(providers).complete({"messages": []}, "req_1")

    assert (error.value.status, error.value.code) == (status, code)
    assert "secret" not in error.value.message
