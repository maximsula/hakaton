import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from .config import PilotConfig, load_config
from .contracts import Accrual, Code, OperationEvent
from .db import Base, Database
from .integrations import DemoRouting, PendingPricing, PricingGateway, RoutingGateway
from .inventory.api import inventory_router
from .operations import read_events
from .orders.api import order_router
from .orders.service import Orders


class AccrualView(BaseModel):
    pricing_pending: bool
    items: list[Accrual]


def create_app(
    db_path: str | None = None, *, config: PilotConfig | None = None,
    routing: RoutingGateway | None = None, pricing: PricingGateway | None = None,
) -> FastAPI:
    config = config or load_config()
    db = Database(db_path or os.environ.get("MESH_DB_PATH", "mesh.sqlite3"))
    routing = routing if routing is not None else DemoRouting(config.demo_routing)
    pricing = pricing if pricing is not None else PendingPricing()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        Base.metadata.create_all(db.engine)
        try:
            yield
        finally:
            db.engine.dispose()

    app = FastAPI(title="Mesh-доставка · Заказы и остатки", version="0.1.0", lifespan=lifespan)
    app.include_router(inventory_router(db, config))
    app.include_router(order_router(Orders(db, config, routing, pricing)))

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/operations")
    def operations(
        point_id: Code | None = None, order_id: Code | None = None, batch_id: Code | None = None,
    ) -> list[OperationEvent]:
        filters = {
            key: value for key, value in {
                "point_id": point_id, "order_id": order_id, "batch_id": batch_id,
            }.items() if value is not None
        }
        with db.transaction() as session:
            return read_events(session, **filters)

    @app.get("/api/accruals")
    def accruals(point_id: Code | None = None) -> AccrualView:
        with db.transaction() as session:
            # A single operation may pay multiple participants, so filter the resulting accruals.
            items = pricing.accruals(read_events(session))
            if point_id is not None:
                items = [item for item in items if item.point_id == point_id]
            return AccrualView(pricing_pending=pricing.pending, items=items)

    return app


app = create_app()
