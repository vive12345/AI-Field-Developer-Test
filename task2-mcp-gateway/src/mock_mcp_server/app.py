"""Mock downstream MCP server speaking JSON-RPC over HTTP (POST /mcp).

It has no security of its own - that is the gateway's job. It records every
message it receives in `app.state.received`, which lets tests prove that
denied requests never reach it.
"""

import json
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from mock_mcp_server.tools import TOOL_DEFINITIONS, TOOL_HANDLERS, ToolFailure

PROTOCOL_VERSION = "2025-11-25"


def create_app() -> FastAPI:
    app = FastAPI(title="Mock MCP Server")
    app.state.received = []

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> Response:
        message = await request.json()
        app.state.received.append({"message": message, "headers": dict(request.headers)})

        if "id" not in message:  # notification: acknowledge, no body
            return Response(status_code=202)

        request_id = message["id"]
        method = message.get("method")
        params = message.get("params", {})

        if method == "initialize":
            return _result(request_id, _initialize_result())
        if method == "tools/list":
            return _result(request_id, {"tools": TOOL_DEFINITIONS})
        if method == "tools/call":
            return _call_tool(request_id, params.get("name"), params.get("arguments") or {})
        return _error(request_id, -32601, f"Method not found: {method}")

    return app


def _call_tool(request_id: Any, name: str, arguments: dict) -> JSONResponse:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return _error(request_id, -32602, f"Unknown tool: {name}")

    try:
        data = handler(arguments)
    except ToolFailure as failure:
        return _result(request_id, {"content": [{"type": "text", "text": str(failure)}], "isError": True})

    return _result(
        request_id,
        {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data},
    )


def _initialize_result() -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "mock-mcp-server", "version": "0.1.0"},
    }


def _result(request_id: Any, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})
