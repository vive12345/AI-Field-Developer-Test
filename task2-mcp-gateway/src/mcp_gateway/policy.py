"""Authorization policy: decide whether an identity may send a JSON-RPC message.

Rules:
- tools/call for a tool whose name starts with "admin_" requires the admin role.
- Every other method (initialize, tools/list, ...) is forwarded unchanged.
"""

from mcp_gateway.auth import Identity, Role
from mcp_gateway.jsonrpc import INVALID_PARAMS, UNAUTHORIZED_TOOL_CALL, JsonRpcError, JsonRpcMessage

TOOLS_CALL = "tools/call"
ADMIN_TOOL_PREFIX = "admin_"


def is_admin_tool(tool_name: str) -> bool:
    # Normalise before comparing so "ADMIN_reset_key" or " admin_reset_key"
    # cannot slip past the gateway if the downstream matches names loosely.
    return tool_name.strip().lower().startswith(ADMIN_TOOL_PREFIX)


def authorize(identity: Identity, message: JsonRpcMessage) -> None:
    """Raise JsonRpcError if the message must not be forwarded."""
    if message.method != TOOLS_CALL:
        return

    tool_name = message.params.get("name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise JsonRpcError(INVALID_PARAMS, "params.name must be a non-empty string", 400, message.id)

    if is_admin_tool(tool_name) and identity.role is not Role.ADMIN:
        raise JsonRpcError(UNAUTHORIZED_TOOL_CALL, "Unauthorized Tool Call", 403, message.id)
