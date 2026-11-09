"""In-memory customer store, seeded from data/customers.json.

Stands in for a real database / payment system. Swapping this module for a
real backend would not require changes to the tools or the server.

Refunds are kept in memory only: they reset when the server restarts and
never modify the JSON seed file.
"""

import json
import uuid
from dataclasses import dataclass
from importlib.resources import files

CUSTOMERS_FILE = files("customer_mcp") / "data" / "customers.json"


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    refundable_balance: float


@dataclass(frozen=True)
class Refund:
    refund_id: str
    customer_id: str
    amount: float
    reason: str
    status: str = "processed"


def _load_customers() -> dict[str, Customer]:
    records = json.loads(CUSTOMERS_FILE.read_text(encoding="utf-8"))
    customers = [Customer(**record) for record in records]
    return {customer.customer_id: customer for customer in customers}


_CUSTOMERS: dict[str, Customer] = _load_customers()
_REFUNDS: list[Refund] = []


def find_customer(customer_id: str) -> Customer | None:
    return _CUSTOMERS.get(customer_id)


def create_refund(customer: Customer, amount: float, reason: str) -> Refund:
    """Record a refund and reduce the customer's refundable balance."""
    refund = Refund(
        refund_id=f"REF-{uuid.uuid4().hex[:8].upper()}",
        customer_id=customer.customer_id,
        amount=amount,
        reason=reason,
    )
    customer.refundable_balance = round(customer.refundable_balance - amount, 2)
    _REFUNDS.append(refund)
    return refund
