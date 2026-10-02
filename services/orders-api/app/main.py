"""
orders-api: accepts orders, reserves stock in inventory-svc, stores the order in
Postgres and publishes an `order.created` event for downstream services.

Order lifecycle: PENDING -> CONFIRMED | REJECTED | FAILED
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response, status
from prometheus_client import Counter
from pydantic import BaseModel, Field

from app import config
from app.db import OrderRepository
from app.events import EventPublisher
from app.inventory_client import InventoryClient, InventoryUnavailable, OutOfStock, UnknownSku
from app.metrics import instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(config.SERVICE_NAME)

ORDERS_TOTAL = Counter("orders_total", "Orders processed, by final status", ["status"])
EVENT_PUBLISH_FAILURES = Counter("order_event_publish_failures_total", "order.created events that failed to publish")

repository = OrderRepository(config.DATABASE_URL)
inventory = InventoryClient(config.INVENTORY_URL)
publisher = EventPublisher(config.RABBITMQ_URL, config.EVENTS_EXCHANGE)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    repository.init_schema()
    yield


app = FastAPI(title="ShopFlow orders-api", lifespan=lifespan)
instrument(app)


# Dependency providers: tests override these with in-memory fakes
def get_repository() -> OrderRepository:
    return repository


def get_inventory() -> InventoryClient:
    return inventory


def get_publisher() -> EventPublisher:
    return publisher


class OrderIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=1000)
    customer_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)


@app.post("/orders", status_code=status.HTTP_201_CREATED)
def create_order(
    order_in: OrderIn,
    repo: OrderRepository = Depends(get_repository),
    inv: InventoryClient = Depends(get_inventory),
    events: EventPublisher = Depends(get_publisher),
) -> dict:
    # Persist first so every attempt is auditable, even if the reservation fails
    order = repo.create(order_in.sku, order_in.quantity, order_in.customer_email, status="PENDING")

    try:
        inv.reserve(order_in.sku, order_in.quantity)
    except OutOfStock:
        _finish(repo, order["id"], "REJECTED")
        raise HTTPException(status.HTTP_409_CONFLICT, f"Not enough stock for {order_in.sku}") from None
    except UnknownSku:
        _finish(repo, order["id"], "REJECTED")
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown SKU {order_in.sku}") from None
    except InventoryUnavailable as exc:
        log.error("inventory-svc unavailable for order %s: %s", order["id"], exc)
        _finish(repo, order["id"], "FAILED")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Inventory service unavailable, try again") from None

    order = _finish(repo, order["id"], "CONFIRMED")

    try:
        events.publish("order.created", order)
    except Exception:  # the order is confirmed; a lost event must not fail the request
        EVENT_PUBLISH_FAILURES.inc()
        log.exception("failed to publish order.created for order %s", order["id"])

    log.info("order %s confirmed: %s x%s", order["id"], order["sku"], order["quantity"])
    return order


def _finish(repo: OrderRepository, order_id: int, final_status: str) -> dict:
    ORDERS_TOTAL.labels(final_status).inc()
    return repo.set_status(order_id, final_status)


@app.get("/orders")
def list_orders(repo: OrderRepository = Depends(get_repository)) -> list[dict]:
    return repo.list()


@app.get("/orders/{order_id}")
def get_order(order_id: int, repo: OrderRepository = Depends(get_repository)) -> dict:
    order = repo.get(order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return order


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """Liveness: the process is up. Deliberately does not check dependencies."""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz(response: Response, repo: OrderRepository = Depends(get_repository)) -> dict:
    """Readiness: only receive traffic while the database is reachable."""
    try:
        repo.ping()
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "reason": str(exc)}
    return {"status": "ready"}
