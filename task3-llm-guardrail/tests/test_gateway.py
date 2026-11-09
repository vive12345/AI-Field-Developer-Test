"""End-to-end gateway tests against the mock provider (in-memory, no network)."""

import json
import re

import httpx
import pytest

from llm_gateway.app import create_app as create_gateway_app
from llm_gateway.config import Settings
from mock_llm_provider.app import RESPONSES
from mock_llm_provider.app import create_app as create_provider_app

pytestmark = pytest.mark.anyio

RAW_PII = [
    "jane.doe@acme-corp.com",
    "j.smith@acme-corp.co.uk",
    "4111 1111 1111 1111",
    "5500-0000-0000-0004",
    "123-45-6789",
    "john.carter@example.com",
    "078-05-1120",
    "378282246310005",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def build_gateway(transport: httpx.AsyncBaseTransport, api_key: str | None = None) -> httpx.AsyncClient:
    app = create_gateway_app(
        settings=Settings(upstream_url="http://provider/v1/chat/completions", upstream_api_key=api_key),
        http_client=httpx.AsyncClient(transport=transport),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


@pytest.fixture
async def gateway():
    async with build_gateway(httpx.ASGITransport(app=create_provider_app(chunk_delay_seconds=0))) as client:
        yield client


def completion_request(prompt: str, stream: bool) -> dict:
    return {"model": "mock-gpt", "stream": stream, "messages": [{"role": "user", "content": prompt}]}


def streamed_text(body: str) -> str:
    text = ""
    for line in body.splitlines():
        data = line.removeprefix("data: ")
        if line.startswith("data: ") and data != "[DONE]":
            for choice in json.loads(data).get("choices", []):
                text += choice["delta"].get("content", "")
    return text


# --- redaction ----------------------------------------------------------------


@pytest.mark.parametrize("keyword", [r["keyword"] for r in RESPONSES])
async def test_streamed_responses_contain_no_raw_pii(gateway, keyword):
    response = await gateway.post("/v1/chat/completions", json=completion_request(keyword, stream=True))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.rstrip().endswith("data: [DONE]")
    text = streamed_text(response.text)
    for value in RAW_PII:
        # Whole-value match: "123-45-6789" inside "123-45-67890" is a different (non-PII) number.
        assert not re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", text), value
    assert "[REDACTED]" in text


async def test_streamed_text_matches_expected_redaction(gateway):
    response = await gateway.post("/v1/chat/completions", json=completion_request("contact", stream=True))

    assert streamed_text(response.text) == (
        "You can reach our support lead at [REDACTED] during business hours. "
        "Her backup is [REDACTED], and general questions go to the help desk."
    )


async def test_non_streamed_response_is_redacted(gateway):
    response = await gateway.post("/v1/chat/completions", json=completion_request("payment", stream=False))

    content = response.json()["choices"][0]["message"]["content"]
    assert content.startswith("The card on file is [REDACTED] and the backup card is [REDACTED].")
    assert "Order 1234567890123" in content  # not a card (fails Luhn), so kept


async def test_invalid_json_body_is_rejected(gateway):
    response = await gateway.post("/v1/chat/completions", content=b"{oops", headers={"Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"


# --- upstream behaviour -------------------------------------------------------


async def test_provider_api_key_is_added_by_the_gateway():
    seen_headers = {}

    def provider(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"choices": []})

    async with build_gateway(httpx.MockTransport(provider), api_key="sk-gateway-secret") as gateway:
        await gateway.post(
            "/v1/chat/completions",
            json=completion_request("hi", stream=False),
            headers={"Authorization": "Bearer client-token"},
        )

    assert seen_headers["authorization"] == "Bearer sk-gateway-secret"


@pytest.mark.parametrize(
    ("upstream_status", "expected_status", "expected_type"),
    [(429, 429, "rate_limited"), (400, 400, "invalid_request"), (500, 502, "upstream_error")],
)
async def test_provider_errors_are_sanitized(upstream_status, expected_status, expected_type):
    def provider(request: httpx.Request) -> httpx.Response:
        return httpx.Response(upstream_status, text="Traceback: internal secret at /srv/model.py line 42")

    async with build_gateway(httpx.MockTransport(provider)) as gateway:
        response = await gateway.post("/v1/chat/completions", json=completion_request("hi", stream=True))

    assert response.status_code == expected_status
    assert response.json()["error"]["type"] == expected_type
    assert "secret" not in response.text and "Traceback" not in response.text


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [(httpx.ConnectError, 502), (httpx.ReadTimeout, 504)],
)
async def test_unreachable_provider(failure, expected_status):
    def provider(request: httpx.Request) -> httpx.Response:
        raise failure("boom", request=request)

    async with build_gateway(httpx.MockTransport(provider)) as gateway:
        response = await gateway.post("/v1/chat/completions", json=completion_request("hi", stream=True))

    assert response.status_code == expected_status
    assert "boom" not in response.text


class _BreaksMidStream(httpx.AsyncByteStream):
    """Sends half an email, then the connection drops."""

    async def __aiter__(self):
        chunk = {"choices": [{"index": 0, "delta": {"content": "Reach me at secret.person@exa"}, "finish_reason": None}]}
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        raise httpx.ReadError("connection reset")


async def test_mid_stream_failure_ends_with_error_event_and_leaks_nothing():
    def provider(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_BreaksMidStream())

    async with build_gateway(httpx.MockTransport(provider)) as gateway:
        response = await gateway.post("/v1/chat/completions", json=completion_request("hi", stream=True))

    assert "secret.person" not in response.text  # held-back partial PII is discarded
    assert '"upstream_interrupted"' in response.text
