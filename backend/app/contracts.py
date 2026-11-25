from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Quantity = Annotated[int, Field(gt=0, le=1_000_000, strict=True)]
Volume = Annotated[int, Field(gt=0, le=1_000_000_000, strict=True)]
RouteType = Literal["shelf", "direct", "hub"]
OrderStatus = Literal[
    "reserved", "packed", "in_transit", "awaiting_hub", "ready",
    "delivered", "cancelled", "awaiting_route", "returned",
]
Action = Literal[
    "pack", "handoff", "receive", "confirm_hub_delivery", "issue", "cancel",
    "shortage", "replan", "update_transit", "return_good", "return_damaged",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class Command(APIModel):
    request_id: Code


class OrderRequest(Command):
    sku: Code
    quantity: Quantity = 1
    wb_club: bool
    pickup_point_id: Code
    promised_by: AwareDatetime


class RoutingStock(APIModel):
    point_id: Code
    available: int


class RoutingRequest(APIModel):
    sku: Code
    quantity: Quantity
    unit_volume_cm3: Volume
    wb_club: bool
    pickup_point_id: Code
    pickup_free_volume_cm3: int
    promised_by: AwareDatetime
    requested_at: AwareDatetime
    stock: list[RoutingStock]
    excluded_source_ids: list[Code] = Field(default_factory=list)


class RouteOption(APIModel):
    route_type: RouteType
    source_point_id: Code | None
    ready_at: AwareDatetime
    estimated_unit_cost_kopecks: int = Field(ge=0)


class RouteDecision(RouteOption):
    pickup_point_id: Code
    reason: str
    alternatives: list[RouteOption] = Field(default_factory=list)


class TransitRequest(APIModel):
    order_id: Code
    pickup_point_id: Code
    actual_position: Code
    observed_at: AwareDatetime
    previous_ready_at: AwareDatetime
    promised_by: AwareDatetime


class TransitEstimate(APIModel):
    ready_at: AwareDatetime
    reason: str


class OperationEvent(APIModel):
    event_id: Code
    type: str
    point_id: Code
    occurred_at: AwareDatetime
    quantity: Quantity
    order_id: Code | None = None
    batch_id: Code | None = None
    sku: Code
    details: dict = Field(default_factory=dict)


class Accrual(APIModel):
    event_id: Code
    point_id: Code
    amount_kopecks: int
    reason: str
