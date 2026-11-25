from datetime import timedelta
from typing import Protocol

from .config import DemoRoutingConfig
from .contracts import (
    Accrual, OperationEvent, RouteDecision, RouteOption, RoutingRequest, RouteType,
    TransitEstimate, TransitRequest,
)


class NoRoute(Exception):
    pass


class RoutingGateway(Protocol):
    def choose(self, request: RoutingRequest) -> RouteDecision: ...

    def estimate_transit(self, request: TransitRequest) -> TransitEstimate: ...


class PricingGateway(Protocol):
    pending: bool

    def accruals(self, events: list[OperationEvent]) -> list[Accrual]: ...


class PendingPricing:
    pending = True

    def accruals(self, events: list[OperationEvent]) -> list[Accrual]:
        return []


class DemoRouting:
    """Temporary deterministic adapter. Travel times and load come from pilot.json."""

    def __init__(self, config: DemoRoutingConfig):
        self.config = config

    def choose(self, request: RoutingRequest) -> RouteDecision:
        config = self.config

        def option(kind: RouteType, source: str | None, minutes: int, cost: int) -> RouteOption:
            return RouteOption(
                route_type=kind, source_point_id=source, estimated_unit_cost_kopecks=cost,
                ready_at=request.requested_at + timedelta(minutes=minutes),
            )

        options = []
        pickup = request.pickup_point_id
        hub = option("hub", None, config.hub_minutes, config.hub_unit_kopecks)
        has_space = request.pickup_free_volume_cm3 >= request.quantity * request.unit_volume_cm3
        if has_space:
            options.append(hub)
        if request.wb_club:
            trip_cost = config.direct_trip_kopecks + config.direct_total_individual_delay_kopecks
            # Round the average trip share half up to whole kopecks.
            share = (2 * trip_cost + config.trip_orders) // (2 * config.trip_orders)
            direct = config.direct_base_unit_kopecks + share + config.retained_hub_unit_kopecks
            for entry in request.stock:
                if entry.available < request.quantity or entry.point_id in request.excluded_source_ids:
                    continue
                if entry.point_id == pickup:
                    options.append(option("shelf", pickup, config.shelf_minutes, config.shelf_unit_kopecks))
                elif has_space:
                    options.append(option("direct", entry.point_id, config.direct_minutes, direct))

        eligible = [candidate for candidate in options if candidate.ready_at <= request.promised_by]
        if not eligible:
            raise NoRoute("По демоданным нет маршрута с доступным остатком, местом и сроком")
        # Local shelf takes priority. Between equally priced hub and direct, choose hub.
        selected = min(eligible, key=lambda route: (
            route.route_type != "shelf", route.estimated_unit_cost_kopecks,
            route.route_type != "hub", route.ready_at, route.source_point_id or "",
        ))
        reasons = {
            "shelf": "Доступный товар можно собрать в выбранном ПВЗ",
            "direct": "Прямой рейс укладывается в срок и выбран по стоимости среди доступных маршрутов",
            "hub": "При текущих условиях выбран хаб среди доступных маршрутов",
        }
        reason = reasons[selected.route_type]
        if not request.wb_club:
            reason = "Для заказа без WB Клуба доступен маршрут через хаб"
        elif selected.route_type == "hub" and any(route.route_type == "direct" for route in eligible):
            reason = "При текущей загрузке хаб стоит столько же или дешевле прямого рейса"
        return RouteDecision(
            **selected.model_dump(), pickup_point_id=pickup, reason=reason, alternatives=eligible,
        )

    def estimate_transit(self, request: TransitRequest) -> TransitEstimate:
        # Demo position is an operator-entered label. The actual ETA belongs to the routing module.
        return TransitEstimate(
            ready_at=max(
                request.previous_ready_at,
                request.observed_at + timedelta(minutes=self.config.direct_minutes),
            ),
            reason=f"Демо-оценка после обновления позиции: {request.actual_position}",
        )
