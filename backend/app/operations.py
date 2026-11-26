import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .contracts import Command, OperationEvent, utc_now
from .models import Operation
from .pricing.ledger import PricingLedger


def reject(code: str, message: str, status: int = 409, *, reason_code: str | None = None):
    detail = {"code": code, "message": message}
    if reason_code is not None:
        detail["reason_code"] = reason_code
    raise HTTPException(status, detail=detail)


class OperationLog:
    def __init__(
        self, session: Session, scope: str, command: Command, ledger: PricingLedger | None = None,
        *, omit_if_none: tuple[str, ...] = (),
    ):
        self.session = session
        self.ledger = ledger
        self.request_id = command.request_id
        data = command.model_dump(mode="json", exclude={"request_id"})
        # Preserve request IDs issued before an optional command field was added.
        for field in omit_if_none:
            if data[field] is None:
                del data[field]
        encoded = json.dumps([scope, data], sort_keys=True).encode()
        self.fingerprint = hashlib.sha256(encoded).hexdigest()

    def replay(self) -> Operation | None:
        existing = self.session.scalar(
            select(Operation).where(Operation.request_id == self.request_id)
        )
        if existing and existing.fingerprint != self.fingerprint:
            reject("request_id_conflict", "Этот идентификатор уже использован для другой операции")
        return existing

    def record(
        self, event_type: str, point_id: str, sku: str, quantity: int, *,
        order_id: str | None = None, batch_id: str | None = None, details: dict | None = None,
    ) -> OperationEvent:
        event = OperationEvent(
            event_id=uuid4().hex, type=event_type, point_id=point_id,
            sku=sku, quantity=quantity, order_id=order_id, batch_id=batch_id,
            occurred_at=utc_now(), details=details or {},
        )
        sequence = self.session.scalar(select(func.max(Operation.sequence))) or 0
        self.session.add(Operation(
            event_id=event.event_id, request_id=self.request_id, fingerprint=self.fingerprint,
            payload=event.model_dump(mode="json"), point_id=point_id, order_id=order_id,
            batch_id=batch_id, occurred_at=event.occurred_at.isoformat(), sequence=sequence + 1,
        ))
        if self.ledger is not None:
            self.session.flush()
            self.ledger.record(self.session, event)
        return event


def read_events(session: Session, **filters) -> list[OperationEvent]:
    records = session.scalars(select(Operation).filter_by(**filters).order_by(Operation.sequence))
    return [OperationEvent.model_validate(record.payload) for record in records]
