"""Mock LLM provider exposing an OpenAI-compatible POST /v1/chat/completions.

The reply is chosen from data/responses.json by keyword in the last user
message, then streamed in deliberately uneven chunks (2-7 characters) so that
emails, SSNs and card numbers get split across chunk boundaries.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from importlib.resources import files
from itertools import cycle

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

RESPONSES: list[dict] = json.loads((files("mock_llm_provider") / "data" / "responses.json").read_text("utf-8"))
CHUNK_SIZES = (3, 5, 2, 7, 4, 6)
MODEL = "mock-gpt"


def create_app(chunk_delay_seconds: float = 0.04) -> FastAPI:
    app = FastAPI(title="Mock LLM Provider")

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        text = pick_response(body.get("messages", []))
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        if body.get("stream"):
            return StreamingResponse(
                stream_chunks(completion_id, text, chunk_delay_seconds),
                media_type="text/event-stream",
            )
        return JSONResponse(full_completion(completion_id, text))

    return app


def pick_response(messages: list[dict]) -> str:
    last_user_message = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    prompt = str(last_user_message).lower()
    for response in RESPONSES:
        if response["keyword"] in prompt:
            return response["text"]
    return next(r["text"] for r in RESPONSES if r["keyword"] == "default")


def split_unevenly(text: str) -> list[str]:
    pieces, position = [], 0
    for size in cycle(CHUNK_SIZES):
        if position >= len(text):
            return pieces
        pieces.append(text[position : position + size])
        position += size


async def stream_chunks(completion_id: str, text: str, delay: float) -> AsyncIterator[bytes]:
    def event(delta: dict, finish_reason: str | None = None) -> bytes:
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(chunk)}\n\n".encode()

    yield event({"role": "assistant", "content": ""})
    for piece in split_unevenly(text):
        await asyncio.sleep(delay)
        yield event({"content": piece})
    yield event({}, finish_reason="stop")
    yield b"data: [DONE]\n\n"


def full_completion(completion_id: str, text: str) -> dict:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }
