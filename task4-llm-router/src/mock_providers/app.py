"""Mock model provider (OpenAI-compatible POST /v1/chat/completions).

The same app runs as the primary or the secondary. Its behaviour can be
switched at runtime, which makes failover easy to demonstrate:

    POST /admin/mode {"mode": "ok" | "rate_limited" | "slow" | "error"}

    ok            200 with a canned reply from data/replies.json and token usage
    rate_limited  429 Too Many Requests
    slow          sleeps `slow_seconds` (default 5s) before answering
    error         500 with a fake stack trace, to prove the gateway never leaks it
"""

import asyncio
import json
import math
import time
import uuid
from importlib.resources import files

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

REPLIES: list[str] = json.loads((files("mock_providers") / "data" / "replies.json").read_text("utf-8"))
MODES = ("ok", "rate_limited", "slow", "error")

FAKE_STACK_TRACE = """Traceback (most recent call last):
  File "/srv/internal/model-server/inference.py", line 214, in generate
    shard = cluster.get_shard(host="10.0.3.17", key="sk-internal-9f8e7d")
RuntimeError: CUDA out of memory on gpu-node-07
"""


def create_app(name: str, mode: str = "ok", slow_seconds: float = 5.0) -> FastAPI:
    app = FastAPI(title=f"Mock provider ({name})")
    app.state.mode = mode
    app.state.calls = 0

    @app.get("/admin/mode")
    async def get_mode() -> dict:
        return {"provider": name, "mode": app.state.mode, "calls": app.state.calls}

    @app.post("/admin/mode")
    async def set_mode(request: Request) -> dict:
        new_mode = (await request.json()).get("mode")
        if new_mode not in MODES:
            raise HTTPException(400, f"mode must be one of {MODES}")
        app.state.mode = new_mode
        return {"provider": name, "mode": new_mode}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        app.state.calls += 1
        body = await request.json()

        if app.state.mode == "rate_limited":
            return JSONResponse({"error": {"message": f"{name}: org quota exceeded"}}, 429, headers={"Retry-After": "20"})
        if app.state.mode == "error":
            return PlainTextResponse(FAKE_STACK_TRACE, 500)
        if app.state.mode == "slow":
            await asyncio.sleep(slow_seconds)

        return JSONResponse(completion(body, name))

    return app


def completion(body: dict, provider_name: str) -> dict:
    messages = body.get("messages", [])
    reply = REPLIES[sum(len(m.get("content", "")) for m in messages) % len(REPLIES)]
    prompt_tokens = sum(4 + math.ceil(len(m.get("content", "")) / 4) for m in messages)
    completion_tokens = math.ceil(len(reply) / 4)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", provider_name),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
