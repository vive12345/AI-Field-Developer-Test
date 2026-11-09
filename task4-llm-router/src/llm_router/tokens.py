"""Request validation and token estimation.

The limiter must decide *before* calling a model, so it reserves an estimate:
prompt tokens (~4 characters per token, plus per-message overhead) + the
completion budget (`max_tokens`). After the call, the reservation is corrected
to the provider's reported `usage.total_tokens`.

In production, replace the character heuristic with the model's tokenizer
(e.g. tiktoken); the rest of the gateway is unaffected.
"""

import math
from typing import Any

from llm_router import errors

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 4
MAX_COMPLETION_TOKENS = 32_000


def validate_request(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise errors.invalid_request("Request body must be a JSON object")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise errors.invalid_request("'messages' must be a non-empty array")
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise errors.invalid_request("Each message must be an object with string 'content'")

    max_tokens = body.get("max_tokens")
    if max_tokens is not None and (
        isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 0 < max_tokens <= MAX_COMPLETION_TOKENS
    ):
        raise errors.invalid_request(f"'max_tokens' must be an integer between 1 and {MAX_COMPLETION_TOKENS}")

    if body.get("stream"):
        raise errors.invalid_request("Streaming is not supported by this router")
    return body


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(MESSAGE_OVERHEAD_TOKENS + math.ceil(len(m["content"]) / CHARS_PER_TOKEN) for m in messages)


def estimate_request_tokens(body: dict[str, Any], default_max_tokens: int) -> int:
    return estimate_prompt_tokens(body["messages"]) + (body.get("max_tokens") or default_max_tokens)
