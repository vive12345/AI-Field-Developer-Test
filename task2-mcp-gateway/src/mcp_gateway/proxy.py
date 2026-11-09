"""Forwarding of authorized messages to the downstream MCP server."""

import logging

import httpx
from starlette.background import BackgroundTask
from starlette.datastructures import Headers
from starlette.responses import StreamingResponse

from mcp_gateway.auth import Identity
from mcp_gateway.jsonrpc import DOWNSTREAM_UNAVAILABLE, JsonRpcError, JsonRpcMessage

logger = logging.getLogger(__name__)

# MCP-relevant headers passed through in each direction. Everything else -
# notably the client's Authorization header - stays at the gateway.
FORWARDED_REQUEST_HEADERS = ("accept", "mcp-session-id", "mcp-protocol-version")
FORWARDED_RESPONSE_HEADERS = ("content-type", "mcp-session-id")


class DownstreamClient:
    def __init__(self, http_client: httpx.AsyncClient, url: str):
        self._http = http_client
        self._url = url

    async def forward(
        self, message: JsonRpcMessage, incoming_headers: Headers, identity: Identity
    ) -> StreamingResponse:
        """Send the message downstream and stream the response back unchanged.

        The parsed payload is re-serialized rather than forwarding raw bytes,
        so the downstream server receives exactly what the gateway authorized.
        """
        headers = {name: incoming_headers[name] for name in FORWARDED_REQUEST_HEADERS if name in incoming_headers}
        headers["x-gateway-user"] = identity.user
        headers["x-gateway-role"] = identity.role.value

        request = self._http.build_request("POST", self._url, json=message.payload, headers=headers)
        try:
            response = await self._http.send(request, stream=True)
        except httpx.TimeoutException:
            logger.warning("Downstream timed out for %s", message.method)
            raise JsonRpcError(DOWNSTREAM_UNAVAILABLE, "Downstream MCP server timed out", 504, message.id) from None
        except httpx.HTTPError as error:
            logger.warning("Downstream unreachable for %s: %s", message.method, error)
            raise JsonRpcError(DOWNSTREAM_UNAVAILABLE, "Downstream MCP server unavailable", 502, message.id) from None

        return StreamingResponse(
            response.aiter_bytes(),  # decoded bytes, so no Content-Encoding to forward
            status_code=response.status_code,
            headers={name: response.headers[name] for name in FORWARDED_RESPONSE_HEADERS if name in response.headers},
            background=BackgroundTask(response.aclose),
        )
