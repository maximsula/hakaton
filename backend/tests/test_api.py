from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.config import load_config
from backend.app.contracts import utc_now
from backend.app.integrations import DemoRouting
from backend.app.main import create_app


def register_points(client, capacity=1000):
    for point in ("A", "B", "C"):
        response = client.put(f"/api/points/{point}", json={"name": f"ПВЗ {point}", "capacity_cm3": capacity})
        assert response.status_code == 200, response.text


@pytest.fixture
def api(tmp_path):
    with TestClient(create_app(str(tmp_path / "test.sqlite3"))) as client:
        register_points(client)
        yield client


def batch_payload(point="A", quantity=3, *, days=1, batch_id="lot", sku="CABLE"):
    return {
        "request_id": uuid4().hex, "point_id": point, "batch_id": batch_id, "sku": sku,
        "seller_id": "seller", "quantity": quantity, "unit_volume_cm3": 100,
        "delivered_at": (utc_now() - timedelta(days=days, minutes=1)).isoformat(),
    }


def receive(client, *args, **kwargs):
    payload = batch_payload(*args, **kwargs)
    response = client.post("/api/batches", json=payload)
    assert response.status_code == 201, response.text
    return payload


def order_payload(point="B", wb_club=True, quantity=1, hours=48):
    return {
        "request_id": uuid4().hex, "sku": "CABLE", "pickup_point_id": point,
        "quantity": quantity, "wb_club": wb_club,
        "promised_by": (utc_now() + timedelta(hours=hours)).isoformat(),
    }


def create(client, *args, **kwargs):
    response = client.post("/api/orders", json=order_payload(*args, **kwargs))
    assert response.status_code == 201, response.text
    return response.json()


def act(client, order, kind, point, expected=200, **extra):
    payload = {
        "request_id": uuid4().hex, "type": kind, "point_id": point, "scan_code": order["scan_code"],
        **extra,
    }
    response = client.post(f"/api/orders/{order['id']}/actions", json=payload)
    assert response.status_code == expected, response.text
    return response.json()


def stock(client, point, batch_id="lot"):
    rows = client.get("/api/inventory", params={"point_id": point}).json()
    return next(row for row in rows if row["batch_id"] == batch_id)


def occupied(client, point):
    return next(row["occupied_volume_cm3"] for row in client.get("/api/points").json() if row["id"] == point)


def test_direct_full_flow_and_repeat_scan(api):
    receive(api)
    order = create(api)
    assert order["route"]["route_type"] == "direct"
    assert order["route"]["estimated_unit_cost_kopecks"] == 16_200
    assert stock(api, "A")["available"] == 2
    assert occupied(api, "A") == 300
    queue = api.get("/api/orders", params={"point_id": "A", "actionable": True}).json()
    assert [row["id"] for row in queue] == [order["id"]]
    assert act(api, order, "pack", "B", 409)["detail"]["code"] == "wrong_point"
    assert act(api, order, "pack", "A", 422, scan_code="wrong")["detail"]["code"] == "wrong_scan_code"
    assert act(api, order, "pack", "A")["status"] == "packed"
    key = uuid4().hex
    act(api, order, "handoff", "A", request_id=key)
    act(api, order, "handoff", "A", request_id=key)
    assert act(api, order, "handoff", "A", 409)["detail"]["code"] == "invalid_order_status"
    assert stock(api, "A")["in_transit"] == 1
    assert occupied(api, "A") == 200
    updated = act(api, order, "update_transit", "A", actual_position="на подъезде к Б")
    assert updated["actual_position"] == "на подъезде к Б"
    assert act(api, order, "receive", "C", 409)["detail"]["code"] == "wrong_point"
    assert act(api, order, "receive", "B")["status"] == "ready"
    assert occupied(api, "B") == 100
    done = act(api, order, "issue", "B")
    assert done["status"] == "delivered"
    assert {action["type"] for action in done["next_actions"]} == {"return_good", "return_damaged"}
    assert done["pricing_pending"] and done["accruals"] == []
    assert [event["type"] for event in done["operations"]] == [
        "order_created", "pack", "handoff", "update_transit", "receive", "issue",
    ]
    assert len({event["event_id"] for event in done["operations"]}) == 6
    assert stock(api, "A")["available"] == 2
    assert stock(api, "A")["in_transit"] == occupied(api, "B") == 0


def test_receipt_capacity_and_idempotency(api):
    accepted = receive(api, quantity=9)
    assert api.post("/api/batches", json=accepted).status_code == 201
    assert api.post("/api/batches", json={**accepted, "request_id": "another"}).status_code == 409
    conflict = api.post("/api/batches", json={**accepted, "quantity": 8})
    assert conflict.json()["detail"]["code"] == "request_id_conflict"
    excess = api.post("/api/batches", json=batch_payload(quantity=1, batch_id="extra"))
    assert excess.json()["detail"]["code"] == "capacity_exceeded"
    assert stock(api, "A")["available"] == 9
    assert len(api.get("/api/operations", params={"point_id": "A"}).json()) == 1


def test_shelf_reserve_and_cancel_across_batches(api):
    receive(api, quantity=1, days=8)
    receive(api, quantity=2, batch_id="new")
    order = create(api, "A", quantity=2)
    assert order["route"]["route_type"] == "shelf"
    assert stock(api, "A")["reserved"] == stock(api, "A", "new")["reserved"] == 1
    assert act(api, order, "pack", "A")["status"] == "ready"
    assert occupied(api, "A") == 300
    assert act(api, order, "cancel", "A")["status"] == "cancelled"
    assert stock(api, "A")["available"] == 1
    assert stock(api, "A", "new")["available"] == 2


def test_hub_confirmation_and_damaged_return(api):
    receive(api)
    order = create(api, wb_club=False)
    assert order["route"]["route_type"] == "hub"
    assert stock(api, "A")["available"] == 3
    assert act(api, order, "confirm_hub_delivery", "B")["status"] == "ready"
    act(api, order, "issue", "B")
    assert act(api, order, "return_damaged", "B")["status"] == "returned"
    batch_id = f"hub-{order['id']}"
    assert stock(api, "B", batch_id)["damaged"] == 1
    response = api.post(f"/api/inventory/B/{batch_id}/actions", json={
        "request_id": "damaged-to-hub", "type": "send_damaged_to_hub", "quantity": 1,
    })
    assert response.status_code == 200, response.text
    assert occupied(api, "B") == 0


def test_good_return_and_slow_stock_transfer(api):
    receive(api, days=40)
    order = create(api, "A")
    act(api, order, "pack", "A")
    act(api, order, "issue", "A")
    act(api, order, "return_good", "A")
    assert stock(api, "A")["available"] == 3
    assert stock(api, "A")["slow_stock"]
    response = api.post("/api/inventory/A/lot/actions", json={
        "request_id": "transfer", "type": "move_slow", "quantity": 2, "destination_point_id": "C",
    })
    assert response.status_code == 200, response.text
    assert stock(api, "A")["available"] == 1
    assert stock(api, "C")["available"] == 2
    assert stock(api, "C")["storage_days"] == 40
    response = api.post("/api/inventory/C/lot/actions", json={
        "request_id": "remove", "type": "remove_slow", "quantity": 1,
    })
    assert response.status_code == 200, response.text
    assert stock(api, "C")["available"] == 1


def test_shortage_blocks_stock_and_replans_to_hub(api):
    receive(api, quantity=1)
    order = create(api)
    rerouted = act(api, order, "shortage", "A", shortage_batch_id="lot")
    assert rerouted["route"]["route_type"] == "hub"
    assert stock(api, "A")["blocked"] == 1
    assert stock(api, "A")["available"] == 0
    reconcile = api.post("/api/inventory/A/lot/actions", json={
        "request_id": "found", "type": "reconcile_found", "quantity": 1,
    })
    assert reconcile.status_code == 200, reconcile.text
    assert stock(api, "A")["available"] == 1
    assert stock(api, "A")["blocked"] == 0


def test_replan_without_route_commits_shortage(api):
    receive(api, quantity=1)
    order = create(api, hours=3)
    result = act(api, order, "shortage", "A")
    assert result["status"] == "awaiting_route"
    assert result["route"] is None and result["route_failure"]
    assert stock(api, "A")["blocked"] == 1
    receive(api, "C", batch_id="replacement")
    assert act(api, order, "replan", "B")["source_point_id"] == "C"


def test_receiving_rechecks_capacity_after_handoff(api):
    receive(api)
    order = create(api, quantity=2)
    act(api, order, "pack", "A")
    act(api, order, "handoff", "A")
    receive(api, "B", 8, batch_id="filler", days=35)
    assert act(api, order, "receive", "B", 409)["detail"]["code"] == "capacity_exceeded"
    assert stock(api, "A")["in_transit"] == 2
    assert api.post("/api/inventory/B/filler/actions", json={
        "request_id": "free-space", "type": "remove_slow", "quantity": 1,
    }).status_code == 200
    assert act(api, order, "receive", "B")["status"] == "ready"


@pytest.mark.parametrize(("trip_orders", "cost", "retained", "route"), [
    (8, 18_500, 0, "hub"),
    (10, 16_200, 0, "direct"),
    (10, 21_400, 5_200, "hub"),
    (10, 17_200, 1_000, "hub"),
])
def test_demo_routing_rates(tmp_path, trip_orders, cost, retained, route):
    config = load_config()
    config.demo_routing.trip_orders = trip_orders
    config.demo_routing.retained_hub_unit_kopecks = retained
    with TestClient(create_app(str(tmp_path / "demo.sqlite3"), config=config)) as client:
        register_points(client)
        receive(client)
        result = create(client)["route"]
        direct = next(option for option in result["alternatives"] if option["route_type"] == "direct")
        assert direct["estimated_unit_cost_kopecks"] == cost
        assert result["route_type"] == route


def test_last_unit_concurrency_recalculates(tmp_path):
    config = load_config()
    barrier = Barrier(2, timeout=5)

    class SynchronizedRouter(DemoRouting):
        calls = 0
        lock = Lock()

        def choose(self, request):
            with self.lock:
                self.calls += 1
                first_snapshot = self.calls <= 2
            if first_snapshot:
                barrier.wait()
            return super().choose(request)

    router = SynchronizedRouter(config.demo_routing)
    with TestClient(create_app(str(tmp_path / "race.sqlite3"), config=config, routing=router)) as client:
        register_points(client)
        receive(client, quantity=1)
        payloads = [order_payload(), order_payload()]
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda payload: client.post("/api/orders", json=payload), payloads))
        assert [response.status_code for response in responses] == [201, 201]
        assert {response.json()["route"]["route_type"] for response in responses} == {"direct", "hub"}
        assert stock(client, "A")["available"] == 0
        assert stock(client, "A")["reserved"] == 1
        assert router.calls == 3
        replay = client.post("/api/orders", json=payloads[0])
        assert replay.json()["id"] == responses[0].json()["id"]
