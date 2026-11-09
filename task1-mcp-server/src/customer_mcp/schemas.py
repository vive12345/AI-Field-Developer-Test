"""Input schemas for every tool.

These Pydantic models are the single source of truth:
- they validate incoming tool arguments, and
- they generate the JSON Schema advertised to clients in tools/list.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"
MAX_REFUND_AMOUNT = 10_000.00

CustomerId = Annotated[
    str,
    Field(
        pattern=CUSTOMER_ID_PATTERN,
        description="Customer ID in the format CUST-XXXXX (exactly 5 digits).",
        examples=["CUST-12345"],
    ),
]

RefundReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=10, max_length=500),
    Field(description="Why the refund is issued (10-500 characters, whitespace trimmed)."),
]


class StrictInput(BaseModel):
    """Base model for tool inputs.

    strict=True     -> no type coercion: "25" is NOT accepted as a number, True is NOT a number.
    extra="forbid"  -> unknown fields (e.g. "admin_override") are rejected.
    """

    model_config = ConfigDict(strict=True, extra="forbid")


class GetCustomerRecordInput(StrictInput):
    customer_id: CustomerId


class TriggerRefundInput(StrictInput):
    customer_id: CustomerId
    amount: float = Field(
        gt=0,
        le=MAX_REFUND_AMOUNT,
        allow_inf_nan=False,
        description="Refund amount, positive, at most 2 decimal places.",
        examples=[25.0],
    )
    reason: RefundReason

    @field_validator("amount")
    @classmethod
    def amount_has_at_most_two_decimals(cls, amount: float) -> float:
        if round(amount, 2) != amount:
            raise ValueError("amount must have at most 2 decimal places")
        return amount
