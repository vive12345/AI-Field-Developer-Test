"""Tests for the SSE stream transformer (timing, per-choice state, edge cases)."""

import asyncio
import json
import time

import pytest

from llm_gateway.guardrail import redact_completion_stream
from llm_gateway.patterns import REDACTED
from llm_gateway.sse import iter_sse_data

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def chunk(content: str | None = None, index: int = 0, finish: str | None = None, role: str | None = None) -> dict:
    delta = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    return {"id": "c1", "model": "m", "choices": [{"index": index, "delta": delta, "finish_reason": finish}]}


async def sse_lines(events: list[dict | str], delay: float = 0.0):
    for event in events:
        if delay:
            await asyncio.sleep(delay)
        yield "data: " + (event if isinstance(event, str) else json.dumps(event))
        yield ""


async def collect(events: list[dict | str], delay: float = 0.0) -> list[dict | str]:
    output = []
    async for raw in redact_completion_stream(iter_sse_data(sse_lines(events, delay))):
        data = raw.decode().removeprefix("data: ").strip()
        output.append(data if data == "[DONE]" else json.loads(data))
    return output


def content_of(output: list, index: int = 0) -> str:
    return "".join(
        choice["delta"].get("content", "")
        for item in output
        if isinstance(item, dict)
        for choice in item.get("choices", [])
        if choice["index"] == index
    )


async def test_split_pii_is_redacted_across_chunks():
    events = [chunk(p) for p in ["Mail jo", "hn@exam", "ple.com", " or SSN 123-", "45-6789", "."]]
    events += [chunk(finish="stop"), "[DONE]"]

    output = await collect(events)

    assert content_of(output) == f"Mail {REDACTED} or SSN {REDACTED}."
    assert output[-1] == "[DONE]"


async def test_first_tokens_are_relayed_before_the_stream_finishes():
    words = [f"word{i} " for i in range(20)]
    events = [chunk(role="assistant", content="")] + [chunk(w) for w in words] + [chunk(finish="stop"), "[DONE]"]
    delay = 0.02  # upstream emits one chunk every 20 ms -> ~460 ms total

    started = time.perf_counter()
    first_content_at = None
    async for raw in redact_completion_stream(iter_sse_data(sse_lines(events, delay))):
        if first_content_at is None and b"word0" in raw:
            first_content_at = time.perf_counter() - started
    total = time.perf_counter() - started

    assert first_content_at is not None
    assert first_content_at < total / 4  # streamed, not buffered until the end


async def test_each_choice_has_independent_state():
    # Interleaved choices: an email split across chunks must not mix with the other choice.
    events = [
        chunk("a: alice@ex", index=0),
        chunk("b: plain", index=1),
        chunk("ample.com", index=0),
        chunk(" text", index=1),
        chunk(finish="stop", index=0),
        chunk(finish="stop", index=1),
        "[DONE]",
    ]

    output = await collect(events)

    assert content_of(output, 0) == f"a: {REDACTED}"
    assert content_of(output, 1) == "b: plain text"


async def test_held_text_is_flushed_when_stream_ends_without_finish_reason():
    output = await collect([chunk("contact bob@corp.io"), "[DONE]"])

    assert content_of(output) == f"contact {REDACTED}"
    assert output[-1] == "[DONE]"


async def test_non_content_chunks_pass_through():
    usage = {"id": "c1", "choices": [], "usage": {"total_tokens": 12}}
    output = await collect([chunk(role="assistant", content=""), usage, "[DONE]"])

    assert output[0]["choices"][0]["delta"]["role"] == "assistant"
    assert usage in output


async def test_non_json_events_are_dropped():
    output = await collect(["not json with john@example.com", chunk("ok"), "[DONE]"])

    assert "john@example.com" not in json.dumps(output)
    assert content_of(output) == "ok"
