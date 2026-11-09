"""Tool: trigger_refund - issue a refund to a customer."""

import logging
from dataclasses import asdict

from customer_mcp import store
from customer_mcp.errors import ToolExecutionError
from customer_mcp.schemas import TriggerRefundInput

logger = logging.getLogger(__name__)


def trigger_refund(args: TriggerRefundInput) -> dict:
    customer = store.find_customer(args.customer_id)
    if customer is None:
        raise ToolExecutionError(f"Customer {args.customer_id} not found")

    if args.amount > customer.refundable_balance:
        raise ToolExecutionError(
            f"Refund of {args.amount:.2f} exceeds refundable balance "
            f"of {customer.refundable_balance:.2f} for {args.customer_id}"
        )

    refund = store.create_refund(customer, args.amount, args.reason)
    logger.info("Refund %s issued: %.2f to %s", refund.refund_id, refund.amount, refund.customer_id)
    return asdict(refund)
