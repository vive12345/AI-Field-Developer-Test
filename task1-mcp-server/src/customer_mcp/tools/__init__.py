"""Tool registry: maps each tool name to its input schema and handler.

To add a new tool: write its schema in schemas.py, its handler in this
package, and add one entry to TOOLS below. The server needs no changes.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import mcp_types as types
from pydantic import BaseModel

from customer_mcp.schemas import GetCustomerRecordInput, TriggerRefundInput
from customer_mcp.tools.get_customer_record import get_customer_record
from customer_mcp.tools.trigger_refund import trigger_refund


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[Any], dict]

    def to_mcp_tool(self) -> types.Tool:
        """Describe this tool for tools/list, using the Pydantic model's JSON Schema."""
        return types.Tool(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
        )


TOOLS: dict[str, ToolDefinition] = {
    tool.name: tool
    for tool in [
        ToolDefinition(
            name="get_customer_record",
            description="Look up a customer's record by customer ID (format CUST-XXXXX).",
            input_model=GetCustomerRecordInput,
            handler=get_customer_record,
        ),
        ToolDefinition(
            name="trigger_refund",
            description="Issue a refund to a customer. Amount must be positive; reason at least 10 characters.",
            input_model=TriggerRefundInput,
            handler=trigger_refund,
        ),
    ]
}
