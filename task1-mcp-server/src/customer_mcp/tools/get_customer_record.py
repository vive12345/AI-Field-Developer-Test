"""Tool: get_customer_record - look up a customer by ID."""

import logging
from dataclasses import asdict

from customer_mcp import store
from customer_mcp.errors import ToolExecutionError
from customer_mcp.schemas import GetCustomerRecordInput

logger = logging.getLogger(__name__)


def get_customer_record(args: GetCustomerRecordInput) -> dict:
    customer = store.find_customer(args.customer_id)
    if customer is None:
        raise ToolExecutionError(f"Customer {args.customer_id} not found")

    logger.info("Fetched customer %s", args.customer_id)
    return asdict(customer)
