"""LLM Gateway HTTP application.

POST /v1/chat/completions (OpenAI-compatible):

    client ──▶ gateway ──▶ LLM provider
           ◀── redacted ◀── SSE chunks, transformed one by one

- stream=true : the provider's SSE stream is redacted chunk-by-chunk and
                relayed immediately (no full-response buffering).
- stream=false: the single JSON response is redacted in one pass.
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from llm_gateway import __version__
from llm_gateway.config import Settings
from llm_gateway.guardrail import redact_completion_stream
from llm_gateway.patterns import redact_text
from llm_gateway.sse import format_sse, iter_sse_data

logger = logging.getLogger(__name__)

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # disable proxy buffering


def create_app(settings: Settings | None = None, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def own_http_client(app: FastAPI):
        async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
            app.state.http = client
            yield

    app = FastAPI(
        title="LLM Gateway (PII guardrail)",
        version=__version__,
        lifespan=own_http_client if http_client is None else None,
    )
    if http_client is not None:
        app.state.http = http_client

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return error_response(400, "invalid_request", "Request body must be valid JSON")
        if not isinstance(body, dict):
            return error_response(400, "invalid_request", "Request body must be a JSON object")

        http: httpx.AsyncClient = request.app.state.http
        upstream_request = http.build_request(
            "POST", settings.upstream_url, json=body, headers=_upstream_headers(settings)
        )
        try:
            upstream = await http.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            return error_response(504, "upstream_timeout", "The model provider did not respond in time")
        except httpx.HTTPError:
            logger.exception("Could not reach the model provider")
            return error_response(502, "upstream_unavailable", "The model provider is unavailable")

        if upstream.status_code != 200:
            return await _upstream_error(upstream)

        if body.get("stream"):
            redacted = redact_completion_stream(iter_sse_data(upstream.aiter_lines()))
            return StreamingResponse(
                _stop_cleanly_on_upstream_failure(redacted),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
                background=BackgroundTask(upstream.aclose),
            )

        try:
            completion = json.loads(await upstream.aread())
        finally:
            await upstream.aclose()
        return JSONResponse(_redact_completion(completion))

    return app


def error_response(status: int, error_type: str, message: str) -> JSONResponse:
    """OpenAI-style error body. Never includes upstream bodies or stack traces."""
    return JSONResponse({"error": {"type": error_type, "message": message}}, status_code=status)


def _upstream_headers(settings: Settings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.upstream_api_key:
        headers["Authorization"] = f"Bearer {settings.upstream_api_key}"
    return headers


async def _upstream_error(upstream: httpx.Response) -> JSONResponse:
    await upstream.aclose()
    logger.warning("Model provider returned HTTP %s", upstream.status_code)
    if upstream.status_code == 429:
        return error_response(429, "rate_limited", "The model provider is rate limiting requests")
    if upstream.status_code in (400, 422):
        return error_response(400, "invalid_request", "The model provider rejected the request")
    return error_response(502, "upstream_error", "The model provider returned an error")


async def _stop_cleanly_on_upstream_failure(stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """If the provider drops mid-stream, end with an error event instead of a broken connection.

    Text still held back inside the redactor is discarded, never flushed unchecked.
    """
    try:
        async for piece in stream:
            yield piece
    except httpx.HTTPError:
        logger.warning("Model provider stream was interrupted")
        error = {"error": {"type": "upstream_interrupted", "message": "The model provider stream was interrupted"}}
        yield format_sse(json.dumps(error))


def _redact_completion(completion: dict) -> dict:
    for choice in completion.get("choices", []):
        message = choice.get("message") or {}
        if isinstance(message.get("content"), str):
            message["content"] = redact_text(message["content"])
    return completion
