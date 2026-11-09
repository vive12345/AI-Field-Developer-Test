"""Mock tool implementations backed by the JSON files in data/.

Nothing is persisted: admin actions report success without changing the data.
"""

import json
from collections.abc import Callable
from importlib.resources import files
from typing import Any

DATA_DIR = files("mock_mcp_server") / "data"


def _load(filename: str) -> Any:
    return json.loads((DATA_DIR / filename).read_text(encoding="utf-8"))


TOOL_DEFINITIONS: list[dict] = _load("tools.json")
CUSTOMERS: dict[str, dict] = {c["customer_id"]: c for c in _load("customers.json")}
ORDERS: list[dict] = _load("orders.json")


class ToolFailure(Exception):
    """The tool ran but could not complete (reported as an isError result)."""


def get_customer(args: dict) -> dict:
    customer = CUSTOMERS.get(args.get("customer_id"))
    if customer is None:
        raise ToolFailure(f"Customer {args.get('customer_id')} not found")
    return customer


def list_orders(args: dict) -> dict:
    customer_id = args.get("customer_id")
    orders = [o for o in ORDERS if customer_id is None or o["customer_id"] == customer_id]
    return {"orders": orders}


def admin_reset_key(args: dict) -> dict:
    return {"key_id": args.get("key_id"), "status": "rotated"}


def admin_delete_customer(args: dict) -> dict:
    if args.get("customer_id") not in CUSTOMERS:
        raise ToolFailure(f"Customer {args.get('customer_id')} not found")
    return {"customer_id": args["customer_id"], "status": "deleted"}


TOOL_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "get_customer": get_customer,
    "list_orders": list_orders,
    "admin_reset_key": admin_reset_key,
    "admin_delete_customer": admin_delete_customer,
}
