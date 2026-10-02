"""inventory-svc tests with an in-memory fake repository."""
import pytest
from fastapi.testclient import TestClient

from app.db import ReserveResult
from app.main import app, get_repository


class FakeRepo:
    def __init__(self):
        self.items = {"SKU-001": {"sku": "SKU-001", "name": "Mechanical Keyboard", "stock": 5}}
        self.healthy = True

    def list(self):
        return list(self.items.values())

    def get(self, sku):
        return self.items.get(sku)

    def reserve(self, sku, quantity):
        item = self.items.get(sku)
        if item is None:
            return ReserveResult.UNKNOWN_SKU, None
        if item["stock"] < quantity:
            return ReserveResult.OUT_OF_STOCK, None
        item["stock"] -= quantity
        return ReserveResult.RESERVED, item["stock"]

    def ping(self):
        if not self.healthy:
            raise ConnectionError("db down")


@pytest.fixture
def repo():
    fake = FakeRepo()
    app.dependency_overrides = {get_repository: lambda: fake}
    yield fake
    app.dependency_overrides = {}


@pytest.fixture
def client(repo):
    return TestClient(app)


def test_reserve_decrements_stock(client, repo):
    resp = client.post("/reserve", json={"sku": "SKU-001", "quantity": 2})
    assert resp.status_code == 200
    assert resp.json() == {"sku": "SKU-001", "reserved": 2, "remaining": 3}
    assert repo.items["SKU-001"]["stock"] == 3


def test_reserve_more_than_stock_is_409_and_stock_unchanged(client, repo):
    resp = client.post("/reserve", json={"sku": "SKU-001", "quantity": 6})
    assert resp.status_code == 409
    assert repo.items["SKU-001"]["stock"] == 5


def test_reserve_unknown_sku_is_404(client):
    assert client.post("/reserve", json={"sku": "NOPE", "quantity": 1}).status_code == 404


def test_reserve_rejects_non_positive_quantity(client):
    assert client.post("/reserve", json={"sku": "SKU-001", "quantity": 0}).status_code == 422


def test_get_and_list_items(client):
    assert client.get("/items/SKU-001").json()["stock"] == 5
    assert client.get("/items/NOPE").status_code == 404
    assert len(client.get("/items").json()) == 1


def test_readiness_reflects_database(client, repo):
    assert client.get("/readyz").status_code == 200
    repo.healthy = False
    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200


def test_metrics_count_reservation_results(client):
    client.post("/reserve", json={"sku": "SKU-001", "quantity": 1})
    client.post("/reserve", json={"sku": "SKU-001", "quantity": 999})
    body = client.get("/metrics").text
    assert 'inventory_reservations_total{result="reserved"}' in body
    assert 'inventory_reservations_total{result="out_of_stock"}' in body
