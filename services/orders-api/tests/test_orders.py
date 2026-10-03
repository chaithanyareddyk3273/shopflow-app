"""orders-api tests with in-memory fakes, so no Postgres, RabbitMQ or inventory-svc is needed."""
import pytest
from fastapi.testclient import TestClient

from app.inventory_client import InventoryUnavailable, OutOfStock, UnknownSku
from app.main import app, get_inventory, get_publisher, get_repository

VALID_ORDER = {"sku": "SKU-001", "quantity": 2, "customer_email": "jane@example.com"}


class FakeRepo:
    def __init__(self):
        self.orders: dict[int, dict] = {}
        self.healthy = True

    def create(self, sku, quantity, customer_email, status):
        order_id = len(self.orders) + 1
        self.orders[order_id] = {
            "id": order_id, "sku": sku, "quantity": quantity,
            "customer_email": customer_email, "status": status, "created_at": "2026-01-01T00:00:00Z",
        }
        return dict(self.orders[order_id])

    def set_status(self, order_id, status):
        self.orders[order_id]["status"] = status
        return dict(self.orders[order_id])

    def get(self, order_id):
        return self.orders.get(order_id)

    def list(self, limit=50):
        return list(self.orders.values())[:limit]

    def ping(self):
        if not self.healthy:
            raise ConnectionError("db down")


class FakeInventory:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.reserved: list[tuple[str, int]] = []

    def reserve(self, sku, quantity):
        if self.error:
            raise self.error
        self.reserved.append((sku, quantity))


class FakePublisher:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.events: list[tuple[str, dict]] = []

    def publish(self, routing_key, payload):
        if self.fail:
            raise ConnectionError("broker down")
        self.events.append((routing_key, payload))


@pytest.fixture
def fakes():
    repo, inv, pub = FakeRepo(), FakeInventory(), FakePublisher()
    app.dependency_overrides = {
        get_repository: lambda: repo,
        get_inventory: lambda: inv,
        get_publisher: lambda: pub,
    }
    yield repo, inv, pub
    app.dependency_overrides = {}


@pytest.fixture
def client(fakes):
    return TestClient(app)  # not used as a context manager, so the DB-init lifespan is skipped


def test_create_order_confirms_reserves_and_publishes(client, fakes):
    repo, inv, pub = fakes
    resp = client.post("/orders", json=VALID_ORDER)

    assert resp.status_code == 201
    assert resp.json()["status"] == "CONFIRMED"
    assert inv.reserved == [("SKU-001", 2)]
    assert pub.events[0][0] == "order.created"
    assert pub.events[0][1]["id"] == resp.json()["id"]


@pytest.mark.parametrize(
    ("error", "http_status", "order_status"),
    [
        (OutOfStock("SKU-001"), 409, "REJECTED"),
        (UnknownSku("SKU-001"), 404, "REJECTED"),
        (InventoryUnavailable("timeout"), 503, "FAILED"),
    ],
)
def test_inventory_errors_map_to_http_and_order_status(client, fakes, error, http_status, order_status):
    repo, inv, pub = fakes
    inv.error = error

    resp = client.post("/orders", json=VALID_ORDER)

    assert resp.status_code == http_status
    assert repo.orders[1]["status"] == order_status
    assert pub.events == []  # no event for orders that were not confirmed


def test_publish_failure_does_not_fail_confirmed_order(client, fakes):
    _, _, pub = fakes
    pub.fail = True

    resp = client.post("/orders", json=VALID_ORDER)

    assert resp.status_code == 201
    assert resp.json()["status"] == "CONFIRMED"


@pytest.mark.parametrize(
    "bad",
    [
        {**VALID_ORDER, "quantity": 0},
        {**VALID_ORDER, "customer_email": "not-an-email"},
        {**VALID_ORDER, "sku": ""},
    ],
)
def test_invalid_input_is_rejected(client, bad):
    assert client.post("/orders", json=bad).status_code == 422


def test_get_order_and_404(client):
    created = client.post("/orders", json=VALID_ORDER).json()
    assert client.get(f"/orders/{created['id']}").json()["sku"] == "SKU-001"
    assert client.get("/orders/999").status_code == 404


def test_readiness_reflects_database(client, fakes):
    repo, _, _ = fakes
    assert client.get("/readyz").status_code == 200
    repo.healthy = False
    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200  # liveness must not depend on the DB


def test_fault_injection_off_by_default(client, fakes):
    repo, _, _ = fakes
    for _ in range(20):
        assert client.post("/orders", json=VALID_ORDER).status_code == 201


def test_fault_injection_fails_requests_with_500(client, fakes, monkeypatch):
    repo, inv, _ = fakes
    monkeypatch.setattr("app.config.FAULT_INJECTION_RATE", 1.0)

    resp = client.post("/orders", json=VALID_ORDER)

    assert resp.status_code == 500
    assert repo.orders == {}       # failed before doing anything: no order saved...
    assert inv.reserved == []      # ...and no stock reserved


def test_metrics_endpoint_exposes_order_counter(client):
    client.post("/orders", json=VALID_ORDER)
    body = client.get("/metrics").text
    assert 'orders_total{status="CONFIRMED"}' in body
    assert "http_request_duration_seconds" in body
