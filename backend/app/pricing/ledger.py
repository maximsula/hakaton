from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts import Accrual, OperationEvent
from ..models import Operation as LoggedOperation
from .accruals import Operation, accruals, read_accruals, record_accrual
from .costs import OperationType, PilotPricing

PAYABLE_OPERATIONS: dict[str, OperationType] = {
    "batch_received": "intake",
    "storage": "storage",
    "pack": "pack",
    "handoff": "handover",
    "receive": "transfer_receive",
    "confirm_hub_delivery": "transfer_receive",
    "issue": "issue",
    "return_good": "return",
    "return_damaged": "return",
}


class PricingLedger:
    def __init__(self, pricing: PilotPricing):
        missing = set(PAYABLE_OPERATIONS.values()) - pricing.operation_rates.keys()
        if missing:
            raise ValueError(f"В конфигурации отсутствуют ставки операций: {', '.join(sorted(missing))}")
        self.pricing = pricing

    def record(self, session: Session, event: OperationEvent) -> None:
        kind = PAYABLE_OPERATIONS.get(event.type)
        if kind is not None:
            record_accrual(session.connection(), Operation(
                operation_id=event.event_id, type=kind, pvz_id=event.point_id,
                occurred_at=event.occurred_at, quantity=event.quantity,
                order_id=event.order_id, batch_id=event.batch_id,
            ), self.pricing)

    def backfill(self, session: Session) -> None:
        missing_events = session.scalars(
            select(LoggedOperation.payload)
            .outerjoin(accruals, LoggedOperation.event_id == accruals.c.operation_id)
            .where(
                accruals.c.operation_id.is_(None),
                LoggedOperation.payload["type"].as_string().in_(PAYABLE_OPERATIONS),
            )
            .order_by(LoggedOperation.sequence)
        )
        for payload in missing_events.all():
            self.record(session, OperationEvent.model_validate(payload))

    def list(
        self, session: Session, *, order_id: str | None = None,
        batch_id: str | None = None, point_id: str | None = None,
    ) -> list[Accrual]:
        return [
            Accrual(
                event_id=entry.operation.operation_id, point_id=entry.operation.pvz_id,
                amount_kopecks=entry.amount_kopecks, reason=entry.operation.type,
                payer_id=entry.payer_id, recipient_id=entry.recipient_id,
                unit_rate_kopecks=entry.unit_rate_kopecks, quantity=entry.operation.quantity,
            )
            for entry in read_accruals(
                session.connection(), order_id=order_id, batch_id=batch_id, pvz_id=point_id,
            )
        ]
