"""MCP server: handles tools/list and tools/call.

We use the low-level Server (not the high-level MCPServer/FastMCP helper)
so we fully control how failures are reported:

    unknown tool            -> JSON-RPC error -32602
    invalid arguments       -> JSON-RPC error -32602 (with field details)
    business failure        -> result with isError: true
    unexpected exception    -> JSON-RPC error -32603 (details only in stderr logs)
"""

import json
import logging

import mcp_types as types
from mcp.server.lowlevel import Server
from pydantic import ValidationError

from customer_mcp import __version__
from customer_mcp.errors import (
    ToolExecutionError,
    internal_error,
    invalid_arguments_error,
    unknown_tool_error,
)
from customer_mcp.tools import TOOLS

logger = logging.getLogger(__name__)

SERVER_NAME = "customer-mcp"


async def handle_list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[tool.to_mcp_tool() for tool in TOOLS.values()])


async def handle_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    logger.info("tools/call received: %s", params.name)

    # 1. Find the tool.
    tool = TOOLS.get(params.name)
    if tool is None:
        logger.warning("Rejected call to unknown tool: %s", params.name)
        raise unknown_tool_error(params.name)

    # 2. Validate the arguments.
    try:
        validated_args = tool.input_model.model_validate(params.arguments or {})
    except ValidationError as error:
        logger.warning("Rejected invalid arguments for %s: %s", tool.name, error.errors())
        raise invalid_arguments_error(error) from None

    # 3. Run the tool.
    try:
        result = tool.handler(validated_args)
    except ToolExecutionError as error:
        logger.info("Tool %s failed: %s", tool.name, error)
        return _error_result(str(error))
    except Exception:
        logger.exception("Unexpected error while running %s", tool.name)
        raise internal_error() from None

    return _success_result(result)


def _success_result(data: dict) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(data))],
        structured_content=data,
    )


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        is_error=True,
    )


def create_server() -> Server:
    return Server(
        SERVER_NAME,
        version=__version__,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )
