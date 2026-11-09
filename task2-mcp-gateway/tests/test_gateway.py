"""End-to-end gateway tests.

The gateway is wired to the real mock downstream app through in-memory ASGI
transports (no network). The mock records every message it receives, so each
test can assert whether a request was forwarded or blocked at the gateway.
"""

import json

import httpx
import pytest

from mcp_gateway.app import create_app as create_gateway_app
from mcp_gateway.config import Settings
from mock_mcp_server.app import create_app as create_downstream_app

pytestmark = pytest.mark.anyio

ADMIN_TOKEN = "admin-token-alice"
VIEWER_TOKEN = "viewer-token-bob"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def downstream():
    return create_downstream_app()


def build_gateway(transport: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    """Return an HTTP client for a gateway whose downstream is reached via `transport`."""
    app = create_gateway_app(
        settings=Settings(downstream_url="http://downstream/mcp"),
        http_client=httpx.AsyncClient(transport=transport),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


@pytest.fixture
async def gateway(downstream):
    async with build_gateway(httpx.ASGITransport(app=downstream)) as client:
        yield client


async def post(client: httpx.AsyncClient, body, token: str | None = None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    content = body if isinstance(body, (bytes, str)) else json.dumps(body)
    return await client.post("/mcp", content=content, headers=headers)


def tools_call(name, arguments=None, request_id=1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def assert_gateway_error(response: httpx.Response, http_status: int, code: int) -> None:
    assert response.status_code == http_status
    assert response.json()["error"]["code"] == code


# --- tools/list: forwarded transparently -------------------------------------


@pytest.mark.parametrize("token", [ADMIN_TOKEN, VIEWER_TOKEN])
async def test_tools_list_is_forwarded_for_any_role(gateway, downstream, token):
    response = await post(gateway, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token)

    assert response.status_code == 200
    tool_names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert tool_names == {"get_customer", "list_orders", "admin_reset_key", "admin_delete_customer"}
    assert len(downstream.state.received) == 1


# --- tools/call: authorization ----------------------------------------------


async def test_viewer_can_call_regular_tool(gateway, downstream):
    response = await post(gateway, tools_call("get_customer", {"customer_id": "CUST-12345"}), VIEWER_TOKEN)

    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["name"] == "Alice Johnson"
    assert len(downstream.state.received) == 1


@pytest.mark.parametrize(
    "tool_name",
    [
        "admin_reset_key",
        "admin_delete_customer",
        "ADMIN_reset_key",  # case variation
        " admin_reset_key",  # leading whitespace
    ],
)
async def test_viewer_is_blocked_from_admin_tools_without_reaching_downstream(gateway, downstream, tool_name):
    response = await post(gateway, tools_call(tool_name, request_id=42), VIEWER_TOKEN)

    assert_gateway_error(response, 403, -32001)
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 42,
        "error": {"code": -32001, "message": "Unauthorized Tool Call"},
    }
    assert downstream.state.received == []


async def test_admin_can_call_admin_tool(gateway, downstream):
    response = await post(gateway, tools_call("admin_reset_key", {"key_id": "KEY-123"}), ADMIN_TOKEN)

    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["status"] == "rotated"
    assert len(downstream.state.received) == 1


async def test_downstream_errors_pass_through_unchanged(gateway):
    response = await post(gateway, tools_call("no_such_tool"), VIEWER_TOKEN)

    assert response.status_code == 200
    assert response.json()["error"] == {"code": -32602, "message": "Unknown tool: no_such_tool"}


# --- authentication ---------------------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Bearer    ",
        "Basic admin-token-alice",
        "admin-token-alice",
        "Bearer not-a-real-token",
    ],
)
async def test_missing_or_invalid_token_is_rejected(gateway, downstream, authorization):
    headers = {"Authorization": authorization} if authorization is not None else {}
    response = await gateway.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers)

    assert_gateway_error(response, 401, -32000)
    assert response.headers["www-authenticate"] == "Bearer"
    assert downstream.state.received == []


# --- JSON-RPC wire format -----------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        pytest.param(b"{not json", -32700, id="invalid-json"),
        pytest.param(b"", -32700, id="empty-body"),
        pytest.param(b'"just a string"', -32600, id="not-an-object"),
        pytest.param(b'{"id": 1, "method": "tools/list"}', -32600, id="missing-jsonrpc"),
        pytest.param(b'{"jsonrpc": "1.0", "id": 1, "method": "tools/list"}', -32600, id="wrong-version"),
        pytest.param(b'{"jsonrpc": "2.0", "id": 1}', -32600, id="missing-method"),
        pytest.param(b'{"jsonrpc": "2.0", "id": true, "method": "tools/list"}', -32600, id="boolean-id"),
        pytest.param(b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}', -32600, id="array-params"),
    ],
)
async def test_malformed_messages_are_rejected(gateway, downstream, body, expected_code):
    response = await post(gateway, body, VIEWER_TOKEN)

    assert_gateway_error(response, 400, expected_code)
    assert downstream.state.received == []


async def test_batch_cannot_smuggle_an_admin_call(gateway, downstream):
    batch = [tools_call("get_customer", {"customer_id": "CUST-12345"}), tools_call("admin_reset_key", request_id=2)]
    response = await post(gateway, batch, VIEWER_TOKEN)

    assert_gateway_error(response, 400, -32600)
    assert downstream.state.received == []


async def test_duplicate_name_key_cannot_smuggle_an_admin_call(gateway, downstream):
    body = (
        b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call",'
        b' "params": {"name": "get_customer", "name": "admin_reset_key"}}'
    )
    response = await post(gateway, body, VIEWER_TOKEN)

    assert_gateway_error(response, 400, -32600)
    assert downstream.state.received == []


@pytest.mark.parametrize("params", [{}, {"name": ""}, {"name": 123}, {"name": None}])
async def test_tools_call_without_valid_tool_name_is_invalid_params(gateway, downstream, params):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    response = await post(gateway, body, ADMIN_TOKEN)

    assert_gateway_error(response, 400, -32602)
    assert downstream.state.received == []


async def test_notifications_are_forwarded(gateway, downstream):
    response = await post(gateway, {"jsonrpc": "2.0", "method": "notifications/initialized"}, VIEWER_TOKEN)

    assert response.status_code == 202
    assert len(downstream.state.received) == 1


# --- forwarding behaviour ---------------------------------------------------


async def test_token_is_not_forwarded_but_identity_is(gateway, downstream):
    await post(gateway, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, VIEWER_TOKEN)

    forwarded_headers = downstream.state.received[0]["headers"]
    assert "authorization" not in forwarded_headers
    assert forwarded_headers["x-gateway-user"] == "bob"
    assert forwarded_headers["x-gateway-role"] == "viewer"


async def test_forwarded_message_matches_what_was_authorized(gateway, downstream):
    message = tools_call("list_orders", {"customer_id": "CUST-12345"}, request_id="abc")
    await post(gateway, message, VIEWER_TOKEN)

    assert downstream.state.received[0]["message"] == message


# --- downstream failures ----------------------------------------------------


async def test_unreachable_downstream_returns_502():
    def refuse_connection(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with build_gateway(httpx.MockTransport(refuse_connection)) as gateway:
        response = await post(gateway, {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, VIEWER_TOKEN)

    assert_gateway_error(response, 502, -32002)
    assert response.json()["id"] == 7


async def test_downstream_timeout_returns_504():
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with build_gateway(httpx.MockTransport(time_out)) as gateway:
        response = await post(gateway, {"jsonrpc": "2.0", "id": 8, "method": "tools/list"}, VIEWER_TOKEN)

    assert_gateway_error(response, 504, -32002)
