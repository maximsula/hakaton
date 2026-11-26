"""Реестр начислений с уникальностью по ID подтверждённой операции."""

from datetime import UTC

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import CheckConstraint, Column, Integer, MetaData, String, Table
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection
from sqlalchemy.sql import select

from .costs import Contract, Count, Identifier, Money, OperationType, PilotPricing


class Operation(Contract):
    operation_id: Identifier
    type: OperationType
    pvz_id: Identifier
    occurred_at: AwareDatetime
    quantity: Count
    order_id: Identifier | None = None
    batch_id: Identifier | None = None

    @model_validator(mode="after")
    def require_reference(self):
        if self.order_id is None and self.batch_id is None:
            raise ValueError("order_id or batch_id is required")
        return self


class Accrual(Contract):
    operation: Operation
    payer_id: Identifier
    recipient_id: Identifier
    unit_rate_kopecks: Money
    amount_kopecks: Money


class AccrualResult(Contract):
    accrual: Accrual
    created: bool


class OperationConflict(ValueError):
    pass


class MissingOperationRate(ValueError):
    pass


class InvalidAccrualAmount(ValueError):
    pass


SQLITE_INTEGER_MAX = (1 << 63) - 1
metadata = MetaData()
accruals = Table(
    "logistics_accruals",
    metadata,
    Column("operation_id", String, primary_key=True),
    Column("operation_type", String, nullable=False),
    Column("pvz_id", String, nullable=False, index=True),
    Column("order_id", String, index=True),
    Column("batch_id", String, index=True),
    Column("payload_json", String, nullable=False),
    Column("amount_kopecks", Integer, nullable=False),
    CheckConstraint("amount_kopecks >= 0", name="ck_logistics_accruals_nonnegative"),
)


class AccrualSummary(Contract):
    entries: tuple[Accrual, ...]
    total_kopecks: Money
    totals_by_recipient: dict[str, Money] = Field(default_factory=dict)


def summarize_accruals(
    connection: Connection,
    *,
    order_id: str | None = None,
    batch_id: str | None = None,
    pvz_id: str | None = None,
) -> AccrualSummary:
    """Возвращает строки и суммы по заказу, партии или отдельному ПВЗ."""
    if all(reference is None for reference in (order_id, batch_id, pvz_id)):
        raise ValueError("order_id, batch_id or pvz_id is required")
    entries = read_accruals(connection, order_id=order_id, batch_id=batch_id, pvz_id=pvz_id)
    totals: dict[str, int] = {}
    for entry in entries:
        totals[entry.recipient_id] = totals.get(entry.recipient_id, 0) + entry.amount_kopecks
    return AccrualSummary(
        entries=entries, total_kopecks=sum(totals.values()), totals_by_recipient=totals
    )


def read_accruals(
    connection: Connection,
    *,
    order_id: str | None = None,
    batch_id: str | None = None,
    pvz_id: str | None = None,
) -> tuple[Accrual, ...]:
    statement = select(accruals.c.payload_json)
    for column, value in (
        (accruals.c.order_id, order_id),
        (accruals.c.batch_id, batch_id),
        (accruals.c.pvz_id, pvz_id),
    ):
        if value is not None:
            statement = statement.where(column == value)
    return tuple(
        sorted(
            (Accrual.model_validate_json(payload) for payload in connection.scalars(statement)),
            key=lambda entry: (entry.operation.occurred_at, entry.operation.operation_id),
        )
    )


def _existing(connection: Connection, operation: Operation) -> AccrualResult | None:
    payload = connection.execute(
        select(accruals.c.payload_json).where(accruals.c.operation_id == operation.operation_id)
    ).scalar_one_or_none()
    if payload is None:
        return None
    accrual = Accrual.model_validate_json(payload)
    if accrual.operation != operation:
        raise OperationConflict(f"Different payload for operation {operation.operation_id}")
    return AccrualResult(accrual=accrual, created=False)


def record_accrual(connection: Connection, operation: Operation, pricing: PilotPricing) -> AccrualResult:
    """Записывает начисление в транзакции подтверждения операции."""
    operation = operation.model_copy(update={"occurred_at": operation.occurred_at.astimezone(UTC)})
    rate = pricing.operation_rates.get(operation.type)
    amount = rate.per_unit_kopecks * operation.quantity if rate else None
    if amount is None or amount > SQLITE_INTEGER_MAX:
        existing = _existing(connection, operation)
        if existing is not None:
            return existing
        if amount is None:
            raise MissingOperationRate(operation.type)
        raise InvalidAccrualAmount("Accrual exceeds SQLite integer range")
    assert rate is not None

    accrual = Accrual(
        operation=operation,
        payer_id=operation.pvz_id if rate.payer_id == "$pvz" else rate.payer_id,
        recipient_id=operation.pvz_id if rate.recipient_id == "$pvz" else rate.recipient_id,
        unit_rate_kopecks=rate.per_unit_kopecks,
        amount_kopecks=amount,
    )
    # Уникальный ключ в БД защищает от повтора даже при параллельной доставке события.
    result = connection.execute(
        insert(accruals)
        .values(
            operation_id=operation.operation_id,
            operation_type=operation.type,
            pvz_id=operation.pvz_id,
            order_id=operation.order_id,
            batch_id=operation.batch_id,
            payload_json=accrual.model_dump_json(),
            amount_kopecks=accrual.amount_kopecks,
        )
        .on_conflict_do_nothing(index_elements=[accruals.c.operation_id])
    )
    if result.rowcount == 1:
        return AccrualResult(accrual=accrual, created=True)
    existing = _existing(connection, operation)
    if existing is None:
        raise RuntimeError("Conflicting accrual disappeared inside the transaction")
    return existing
