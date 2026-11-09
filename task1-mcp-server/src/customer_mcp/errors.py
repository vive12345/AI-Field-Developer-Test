"""Error types and their mapping to MCP / JSON-RPC.

Two kinds of failure exist in MCP:

1. Protocol errors -> JSON-RPC `error` object (raised as MCPError).
   Used for invalid arguments, unknown tools, and unexpected crashes.

2. Tool execution errors -> normal result with `isError: true`.
   Used when input is valid but the business operation fails
   (e.g. customer not found). Raised as ToolExecutionError.
"""

from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR, INVALID_PARAMS
from pydantic import ValidationError


class ToolExecutionError(Exception):
    """Valid input, but the operation could not be completed."""


def invalid_arguments_error(error: ValidationError) -> MCPError:
    """Convert a Pydantic ValidationError into a JSON-RPC -32602 error."""
    details = [
        {
            "field": ".".join(str(part) for part in issue["loc"]) or "arguments",
            "issue": issue["msg"],
        }
        for issue in error.errors()
    ]
    return MCPError(code=INVALID_PARAMS, message="Invalid tool arguments", data=details)


def unknown_tool_error(tool_name: str) -> MCPError:
    return MCPError(code=INVALID_PARAMS, message=f"Unknown tool: {tool_name}")


def internal_error() -> MCPError:
    # Deliberately generic: internal details are logged to stderr, never sent to the client.
    return MCPError(code=INTERNAL_ERROR, message="Internal server error")
