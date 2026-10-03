"""
orders-api: the entry point for customers placing orders.

What it does for each order:
  1. Saves the order in its own Postgres database with status PENDING
  2. Asks inventory-svc to reserve the stock (synchronous HTTP call)
  3. Marks the order CONFIRMED, or REJECTED / FAILED if the reservation didn't work
  4. Publishes an `order.created` event to RabbitMQ so other services (the notifier) can react

Order lifecycle: PENDING -> CONFIRMED | REJECTED | FAILED
"""
import logging
import random
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

# ── Prometheus metrics (exposed on /metrics) ────────────────────────────────────
# A Counter only goes up. Labels split it into series, e.g. orders_total{status="CONFIRMED"}.
ORDERS_TOTAL = Counter("orders_total", "Orders processed, by final status", ["status"])
EVENT_PUBLISH_FAILURES = Counter("order_event_publish_failures_total", "order.created events that failed to publish")

# ── Connections to the outside world ────────────────────────────────────────────
# Created once when the app starts. Nothing connects yet; each call opens its own connection.
repository = OrderRepository(config.DATABASE_URL)
inventory = InventoryClient(config.INVENTORY_URL)
publisher = EventPublisher(config.RABBITMQ_URL, config.EVENTS_EXCHANGE)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Runs once at startup (before `yield`) and once at shutdown (after `yield`)."""
    repository.init_schema()  # create the `orders` table if it doesn't exist yet
    yield


app = FastAPI(title="ShopFlow orders-api", lifespan=lifespan)
instrument(app)  # adds request-latency metrics and the /metrics endpoint


# ── Dependency providers ────────────────────────────────────────────────────────
# Endpoints receive their database/inventory/publisher through FastAPI's `Depends(...)`
# instead of using the globals directly. That lets the tests swap in in-memory fakes
# (see tests/test_orders.py) so they run without Postgres, RabbitMQ or inventory-svc.
def get_repository() -> OrderRepository:
    return repository


def get_inventory() -> InventoryClient:
    return inventory


def get_publisher() -> EventPublisher:
    return publisher


class OrderIn(BaseModel):
    """
    The JSON body a client must send to POST /orders.
    Pydantic validates it automatically; invalid input gets a 422 response
    before our code even runs.
    """
    sku: str = Field(min_length=1, max_length=64)        # product code, e.g. "SKU-001"
    quantity: int = Field(gt=0, le=1000)                 # 1..1000
    customer_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)


@app.post("/orders", status_code=status.HTTP_201_CREATED)
def create_order(
    order_in: OrderIn,
    repo: OrderRepository = Depends(get_repository),
    inv: InventoryClient = Depends(get_inventory),
    events: EventPublisher = Depends(get_publisher),
) -> dict:
    # Chaos testing only (off by default): fail a share of requests on purpose
    if config.FAULT_INJECTION_RATE and random.random() < config.FAULT_INJECTION_RATE:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Injected fault (FAULT_INJECTION_RATE)")

    # Step 1: save the order as PENDING first, so every attempt is recorded,
    # even ones that end up rejected or failed.
    order = repo.create(order_in.sku, order_in.quantity, order_in.customer_email, status="PENDING")

    # Step 2: reserve the stock. Each possible problem maps to a clear HTTP error.
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

    # Step 3: stock is reserved, so the order is confirmed.
    order = _finish(repo, order["id"], "CONFIRMED")

    # Step 4: tell the rest of the system. If this fails, the customer's order still
    # stands (stock is already reserved), so we record the failure instead of returning an error.
    try:
        events.publish("order.created", order)
    except Exception:
        EVENT_PUBLISH_FAILURES.inc()
        log.exception("failed to publish order.created for order %s", order["id"])

    log.info("order %s confirmed: %s x%s", order["id"], order["sku"], order["quantity"])
    return order


def _finish(repo: OrderRepository, order_id: int, final_status: str) -> dict:
    """Set an order's final status and count it in the orders_total metric."""
    ORDERS_TOTAL.labels(final_status).inc()
    return repo.set_status(order_id, final_status)


@app.get("/orders")
def list_orders(repo: OrderRepository = Depends(get_repository)) -> list[dict]:
    """The 50 most recent orders."""
    return repo.list()


@app.get("/orders/{order_id}")
def get_order(order_id: int, repo: OrderRepository = Depends(get_repository)) -> dict:
    order = repo.get(order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return order


# ── Health checks used by Kubernetes ────────────────────────────────────────────
@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """
    Liveness: "is the process alive?" If this fails, Kubernetes restarts the pod.
    It deliberately does NOT check the database: if Postgres is down, restarting
    this pod wouldn't help and would only cause a restart storm.
    """
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz(response: Response, repo: OrderRepository = Depends(get_repository)) -> dict:
    """
    Readiness: "can this pod handle requests right now?" If this fails, Kubernetes
    stops sending traffic to the pod (without restarting it) until the database is back.
    """
    try:
        repo.ping()
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "reason": str(exc)}
    return {"status": "ready"}
