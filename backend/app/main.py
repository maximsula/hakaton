import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from .config import PilotConfig, load_config
from .contracts import Accrual, Code, OperationEvent
from .db import Base, Database
from .integrations import PilotRouting
from .inventory.api import inventory_router
from .operations import read_events
from .orders.api import order_router
from .orders.service import Orders
from .pricing.accruals import metadata as accrual_metadata
from .pricing.ledger import PricingLedger


class AccrualView(BaseModel):
    pricing_pending: bool
    items: list[Accrual]
    total_kopecks: int


def create_app(
    db_path: str | None = None, *, config: PilotConfig | None = None,
    routing: PilotRouting | None = None,
) -> FastAPI:
    config = config or load_config()
    db = Database(db_path or os.environ.get("MESH_DB_PATH", "mesh.sqlite3"))
    routing = routing if routing is not None else PilotRouting(config.demo_routing, config.logistics_pricing)
    ledger = PricingLedger(config.logistics_pricing)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            with db.transaction(write=True) as session:
                Base.metadata.create_all(session.connection())
                accrual_metadata.create_all(session.connection())
                ledger.backfill(session)
            yield
        finally:
            db.engine.dispose()

    app = FastAPI(title="Mesh-доставка · Заказы, остатки и логистика", version="0.2.0", lifespan=lifespan)
    app.include_router(inventory_router(db, config, ledger))
    app.include_router(order_router(Orders(db, config, routing, ledger)))

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
    def accruals(
        point_id: Code | None = None, order_id: Code | None = None, batch_id: Code | None = None,
    ) -> AccrualView:
        with db.transaction() as session:
            items = ledger.list(session, point_id=point_id, order_id=order_id, batch_id=batch_id)
            return AccrualView(
                pricing_pending=False, items=items, total_kopecks=sum(item.amount_kopecks for item in items),
            )

    return app


app = create_app()
