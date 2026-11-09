"""End-to-end tests: start the real server as a subprocess and talk raw JSON-RPC over stdio.

Talking raw JSON-RPC (instead of using an MCP client library) lets us verify
two things at once:
- protocol behaviour (exact error codes, isError results), and
- stdout purity: every line the server writes to stdout must be JSON-RPC.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROTOCOL_VERSION = "2025-11-25"
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601

VALID_REFUND = {"customer_id": "CUST-12345", "amount": 25.0, "reason": "Product arrived damaged"}


class StdioClient:
    """Minimal MCP client speaking newline-delimited JSON-RPC to a server subprocess."""

    def __init__(self, stderr_file: Path):
        self._stderr = stderr_file.open("w")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "customer_mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
        )
        self._next_id = 1

    def send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        """Send a request and return the matching response.

        json.loads on every line doubles as a stdout purity check:
        any non-JSON output makes the test fail immediately.
        """
        request_id = self._next_id
        self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})

        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("Server closed stdout unexpectedly")
            message = json.loads(line)
            assert message["jsonrpc"] == "2.0"
            if message.get("id") == request_id:
                return message

    def initialize(self) -> dict:
        response = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def call_tool(self, name: str, arguments: dict | None) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> str:
        """Close stdin (ends the session), wait for exit, return any leftover stdout."""
        remaining_stdout, _ = self.process.communicate(timeout=5)
        self._stderr.close()
        return remaining_stdout


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> StdioClient:
    client = StdioClient(tmp_path_factory.mktemp("logs") / "stderr.log")
    client.initialize()
    yield client
    client.close()


def assert_invalid_params(response: dict) -> None:
    assert "result" not in response
    assert response["error"]["code"] == INVALID_PARAMS


# --- tools/list -------------------------------------------------------------


def test_tools_list_returns_both_tools_with_strict_schemas(client):
    tools = {tool["name"]: tool for tool in client.request("tools/list")["result"]["tools"]}

    assert set(tools) == {"get_customer_record", "trigger_refund"}
    for tool in tools.values():
        assert tool["inputSchema"]["additionalProperties"] is False

    refund_schema = tools["trigger_refund"]["inputSchema"]
    assert set(refund_schema["required"]) == {"customer_id", "amount", "reason"}


# --- get_customer_record ----------------------------------------------------


def test_get_customer_record_returns_customer(client):
    result = client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})["result"]

    assert not result.get("isError")
    assert result["structuredContent"]["name"] == "Alice Johnson"


def test_customers_are_loaded_from_json_data_file(client):
    seed_file = Path(__file__).parents[1] / "src" / "customer_mcp" / "data" / "customers.json"
    seed_customers = json.loads(seed_file.read_text())

    for expected in seed_customers:
        result = client.call_tool("get_customer_record", {"customer_id": expected["customer_id"]})
        assert result["result"]["structuredContent"]["name"] == expected["name"]


def test_unknown_customer_is_a_tool_error_not_a_protocol_error(client):
    response = client.call_tool("get_customer_record", {"customer_id": "CUST-99999"})

    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "not found" in response["result"]["content"][0]["text"]


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"customer_id": "USER-12345"}, id="wrong-prefix"),
        pytest.param({"customer_id": "cust-12345"}, id="lowercase-prefix"),
        pytest.param({"customer_id": "CUST-1234"}, id="too-few-digits"),
        pytest.param({"customer_id": "CUST-123456"}, id="too-many-digits"),
        pytest.param({"customer_id": "CUST-1234A"}, id="non-digit"),
        pytest.param({"customer_id": " CUST-12345"}, id="leading-space"),
        pytest.param({"customer_id": 12345}, id="number-instead-of-string"),
        pytest.param({"customer_id": None}, id="null"),
        pytest.param({"customer_id": {"id": "CUST-12345"}}, id="nested-object"),
        pytest.param({}, id="missing-field"),
        pytest.param(None, id="no-arguments"),
        pytest.param({"customer_id": "CUST-12345", "admin_override": True}, id="extra-field"),
    ],
)
def test_get_customer_record_rejects_invalid_input(client, arguments):
    assert_invalid_params(client.call_tool("get_customer_record", arguments))


# --- trigger_refund ---------------------------------------------------------


def test_trigger_refund_succeeds(client):
    result = client.call_tool("trigger_refund", VALID_REFUND)["result"]

    assert not result.get("isError")
    assert result["structuredContent"]["status"] == "processed"
    assert result["structuredContent"]["refund_id"].startswith("REF-")


def test_trigger_refund_accepts_integer_amount(client):
    result = client.call_tool("trigger_refund", {**VALID_REFUND, "amount": 10})["result"]

    assert not result.get("isError")


def test_refund_above_balance_is_a_tool_error(client):
    response = client.call_tool("trigger_refund", {**VALID_REFUND, "customer_id": "CUST-24680"})

    assert response["result"]["isError"] is True
    assert "exceeds refundable balance" in response["result"]["content"][0]["text"]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"amount": -20}, id="negative-amount"),
        pytest.param({"amount": 0}, id="zero-amount"),
        pytest.param({"amount": "25"}, id="string-amount"),
        pytest.param({"amount": True}, id="boolean-amount"),
        pytest.param({"amount": None}, id="null-amount"),
        pytest.param({"amount": 10.999}, id="three-decimals"),
        pytest.param({"amount": 1_000_000}, id="above-maximum"),
        pytest.param({"reason": "broken"}, id="short-reason"),
        pytest.param({"reason": "            "}, id="whitespace-reason"),
        pytest.param({"reason": "   broken   "}, id="short-after-trim"),
        pytest.param({"reason": 1234567890}, id="number-reason"),
        pytest.param({"reason": ["Product arrived damaged"]}, id="list-reason"),
        pytest.param({"customer_id": "CUST-1"}, id="bad-customer-id"),
        pytest.param({"admin_override": True}, id="extra-field"),
    ],
)
def test_trigger_refund_rejects_invalid_input(client, overrides):
    assert_invalid_params(client.call_tool("trigger_refund", {**VALID_REFUND, **overrides}))


@pytest.mark.parametrize("missing_field", ["customer_id", "amount", "reason"])
def test_trigger_refund_rejects_missing_field(client, missing_field):
    arguments = {key: value for key, value in VALID_REFUND.items() if key != missing_field}
    assert_invalid_params(client.call_tool("trigger_refund", arguments))


def test_invalid_params_error_names_the_bad_field(client):
    response = client.call_tool("trigger_refund", {**VALID_REFUND, "amount": -5})

    fields = [detail["field"] for detail in response["error"]["data"]]
    assert fields == ["amount"]


# --- protocol-level errors --------------------------------------------------


def test_unknown_tool_is_invalid_params(client):
    assert_invalid_params(client.call_tool("delete_customer", {}))


def test_unknown_method_is_method_not_found(client):
    response = client.request("customers/delete")

    assert response["error"]["code"] == METHOD_NOT_FOUND


# --- stdio isolation --------------------------------------------------------


def test_stdout_is_pure_json_rpc_and_logs_go_to_stderr(tmp_path):
    stderr_file = tmp_path / "stderr.log"
    client = StdioClient(stderr_file)

    client.initialize()
    client.request("tools/list")
    client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
    client.call_tool("get_customer_record", {"customer_id": "bad"})
    client.call_tool("trigger_refund", VALID_REFUND)
    leftover_stdout = client.close()

    # Nothing besides the responses we already parsed may appear on stdout.
    for line in leftover_stdout.splitlines():
        assert json.loads(line)["jsonrpc"] == "2.0"

    # Logs exist - and they went to stderr.
    logs = stderr_file.read_text()
    assert "Customer MCP server started" in logs
    assert "tools/call received: trigger_refund" in logs
