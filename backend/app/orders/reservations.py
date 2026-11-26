from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..contracts import ResourceUsage, RouteResource, utc_now
from ..models import Order, RouteReservation
from ..operations import reject


def resource_usage(session: Session, now: datetime) -> dict[str, ResourceUsage]:
    totals: dict[str, ResourceUsage] = {}
    rows = session.scalars(
        select(RouteReservation).where(RouteReservation.expires_at > now.isoformat())
        .order_by(RouteReservation.resource_key, RouteReservation.order_index)
    )
    for row in rows:
        usage = totals.setdefault(row.resource_key, ResourceUsage())
        usage.quantity += row.quantity
        if row.order_index is not None:
            usage.order_indices.append(row.order_index)
    return totals


def reserve(session: Session, order: Order, resource: RouteResource) -> None:
    session.add(RouteReservation(
        order_id=order.id, stage=resource.stage, resource_key=resource.key,
        expires_at=resource.expires_at.isoformat(), quantity=order.quantity,
        order_index=resource.order_index,
    ))


def release_pending(session: Session, order: Order, stage: str | None = None) -> None:
    statement = delete(RouteReservation).where(
        RouteReservation.order_id == order.id, RouteReservation.consumed.is_(False),
    )
    if stage is not None:
        statement = statement.where(RouteReservation.stage == stage)
    session.execute(statement)


def consume(session: Session, order: Order, *stages: str) -> None:
    reservations = session.scalars(select(RouteReservation).where(
        RouteReservation.order_id == order.id, RouteReservation.consumed.is_(False),
        RouteReservation.stage.in_(stages),
    ))
    for row in reservations:
        if datetime.fromisoformat(row.expires_at) <= utc_now():
            action = "обновите положение посылки" if row.stage == "receive" else "пересчитайте маршрут"
            reject("route_window_expired", f"Срок зарезервированного окна или рейса истёк, {action}")
        row.consumed = True
