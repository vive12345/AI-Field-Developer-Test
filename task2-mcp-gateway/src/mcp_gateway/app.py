"""Gateway HTTP application.

Every POST /mcp request goes through the same pipeline:

    1. authenticate  - Bearer token -> Identity          (401 / -32000 on failure)
    2. parse         - body -> validated JSON-RPC message (400 / -32700, -32600)
    3. authorize     - role vs. method / tool name        (403 / -32001 on denial)
    4. forward       - stream the downstream response back (502 / 504 if unreachable)

Steps 1-3 reject the request without ever contacting the downstream server.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from mcp_gateway import __version__
from mcp_gateway.auth import TokenStore, authenticate
from mcp_gateway.config import Settings
from mcp_gateway.jsonrpc import JsonRpcError, parse_message
from mcp_gateway.policy import authorize
from mcp_gateway.proxy import DownstreamClient

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    token_store: TokenStore | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the gateway.

    Pass `http_client` to control how the downstream is reached (used by tests);
    otherwise the app creates and closes its own client.
    """
    settings = settings or Settings.from_env()
    token_store = token_store or TokenStore.from_file()

    @asynccontextmanager
    async def own_http_client(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(timeout=settings.downstream_timeout_seconds) as client:
            app.state.downstream = DownstreamClient(client, settings.downstream_url)
            yield

    app = FastAPI(
        title="MCP Security Gateway",
        version=__version__,
        lifespan=own_http_client if http_client is None else None,
    )
    if http_client is not None:
        app.state.downstream = DownstreamClient(http_client, settings.downstream_url)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> Response:
        try:
            identity = authenticate(request.headers.get("authorization"), token_store)
            message = parse_message(await request.body())
            authorize(identity, message)
        except JsonRpcError as error:
            logger.warning("Rejected request: %s (code %s)", error.message, error.code)
            return _error_response(error)

        logger.info(
            "Forwarding %s%s for user=%s role=%s",
            message.method,
            f" ({message.params['name']})" if message.method == "tools/call" else "",
            identity.user,
            identity.role.value,
        )
        try:
            return await request.app.state.downstream.forward(message, request.headers, identity)
        except JsonRpcError as error:
            return _error_response(error)

    return app


def _error_response(error: JsonRpcError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if error.http_status == 401 else None
    return JSONResponse(error.to_response(), status_code=error.http_status, headers=headers)
