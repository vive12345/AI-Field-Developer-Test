"""JSON-RPC 2.0 wire format: parsing incoming messages and building error responses."""

import json
from dataclasses import dataclass
from typing import Any

# Standard JSON-RPC codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Gateway-specific codes (JSON-RPC reserves -32000..-32099 for implementation-defined errors)
UNAUTHENTICATED = -32000
UNAUTHORIZED_TOOL_CALL = -32001
DOWNSTREAM_UNAVAILABLE = -32002

RequestId = str | int | None


class JsonRpcError(Exception):
    """An error the gateway answers itself, without contacting the downstream server."""

    def __init__(self, code: int, message: str, http_status: int, request_id: RequestId = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.request_id = request_id

    def to_response(self) -> dict[str, Any]:
        return error_response(self.code, self.message, self.request_id)


@dataclass(frozen=True)
class JsonRpcMessage:
    """A validated JSON-RPC request or notification."""

    payload: dict[str, Any]

    @property
    def method(self) -> str:
        return self.payload["method"]

    @property
    def id(self) -> RequestId:
        return self.payload.get("id")

    @property
    def params(self) -> dict[str, Any]:
        return self.payload.get("params", {})


def error_response(code: int, message: str, request_id: RequestId = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def parse_message(body: bytes) -> JsonRpcMessage:
    """Parse and validate a single JSON-RPC message. Raises JsonRpcError on any problem."""
    try:
        payload = json.loads(body, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKeyError as error:
        raise JsonRpcError(INVALID_REQUEST, str(error), http_status=400) from None
    except (ValueError, UnicodeDecodeError):
        raise JsonRpcError(PARSE_ERROR, "Parse error: body is not valid JSON", http_status=400) from None

    if isinstance(payload, list):
        # MCP (2025-06-18+) removed JSON-RPC batching; rejecting batches also
        # prevents hiding an admin call among allowed ones.
        raise JsonRpcError(INVALID_REQUEST, "Batch requests are not supported", http_status=400)

    if not isinstance(payload, dict):
        raise JsonRpcError(INVALID_REQUEST, "Request must be a JSON object", http_status=400)

    request_id = payload.get("id")
    if not _is_valid_id(request_id):
        raise JsonRpcError(INVALID_REQUEST, "Invalid id: must be a string, integer or null", http_status=400)

    if payload.get("jsonrpc") != "2.0":
        raise JsonRpcError(INVALID_REQUEST, 'jsonrpc must be exactly "2.0"', 400, request_id)

    if not isinstance(payload.get("method"), str):
        raise JsonRpcError(INVALID_REQUEST, "method must be a string", 400, request_id)

    if not isinstance(payload.get("params", {}), dict):
        raise JsonRpcError(INVALID_REQUEST, "params must be an object", 400, request_id)

    return JsonRpcMessage(payload)


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse objects like {"name": "get_customer", "name": "admin_reset_key"}.

    Different JSON parsers keep different duplicates, so the gateway and the
    downstream server could disagree on which tool is being called.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"Duplicate key in JSON object: {key!r}")
        result[key] = value
    return result


def _is_valid_id(value: Any) -> bool:
    # bool is a subclass of int in Python, so exclude it explicitly.
    return value is None or (isinstance(value, (str, int)) and not isinstance(value, bool))
