"""Apply PII redaction to an OpenAI-compatible chat-completion stream.

Input:  SSE `data` payloads from the provider (JSON chunks, then "[DONE]").
Output: SSE bytes for the client, with `choices[i].delta.content` redacted.

Each chunk is transformed and yielded immediately; only the small held-back
tail inside each StreamingRedactor survives between chunks.
"""

import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

from llm_gateway.redactor import StreamingRedactor
from llm_gateway.sse import format_sse

logger = logging.getLogger(__name__)

DONE = "[DONE]"


async def redact_completion_stream(events: AsyncIterator[str]) -> AsyncIterator[bytes]:
    # One redactor per choice index, so parallel completions (n > 1) never mix text.
    redactors: dict[int, StreamingRedactor] = {}
    last_chunk: dict[str, Any] = {}

    async for data in events:
        if data == DONE:
            break

        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            # Unknown payloads could contain anything, so they are dropped, not forwarded.
            logger.warning("Dropped non-JSON stream event (%d bytes)", len(data))
            continue

        for choice in chunk.get("choices", []):
            _redact_choice(choice, redactors)

        last_chunk = chunk
        if _has_payload(chunk):
            yield _encode(chunk)

    # Release whatever is still held back (choices that never sent finish_reason).
    for chunk in _flush_remaining(redactors, last_chunk):
        yield _encode(chunk)
    yield format_sse(DONE)


def _redact_choice(choice: dict[str, Any], redactors: dict[int, StreamingRedactor]) -> None:
    index = choice.get("index", 0)
    redactor = redactors.setdefault(index, StreamingRedactor())
    delta = choice.get("delta") or {}

    safe_text = redactor.feed(delta["content"]) if delta.get("content") else ""
    if choice.get("finish_reason"):
        safe_text += redactor.flush()
        del redactors[index]

    if "content" in delta or safe_text:
        delta["content"] = safe_text
    choice["delta"] = delta


def _has_payload(chunk: dict[str, Any]) -> bool:
    """Skip chunks whose only content was fully held back - there is nothing to send yet."""
    choices = chunk.get("choices")
    if not choices:
        return True  # e.g. a usage-only chunk
    for choice in choices:
        delta = choice.get("delta", {})
        if choice.get("finish_reason") or delta.get("content") or set(delta) - {"content"}:
            return True
    return False


def _flush_remaining(redactors: dict[int, StreamingRedactor], template: dict[str, Any]) -> Iterator[dict]:
    for index, redactor in redactors.items():
        if text := redactor.flush():
            yield {**template, "choices": [{"index": index, "delta": {"content": text}, "finish_reason": None}]}


def _encode(chunk: dict[str, Any]) -> bytes:
    return format_sse(json.dumps(chunk, ensure_ascii=False, separators=(",", ":")))
