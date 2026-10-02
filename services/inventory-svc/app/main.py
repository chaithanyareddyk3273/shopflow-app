"""inventory-svc: owns product stock and reserves it for orders."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response, status
from prometheus_client import Counter
from pydantic import BaseModel, Field

from app.db import InventoryRepository, ReserveResult
from app.metrics import instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("inventory-svc")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shopflow:shopflow@localhost:5432/inventory")

RESERVATIONS_TOTAL = Counter("inventory_reservations_total", "Stock reservation attempts, by result", ["result"])

repository = InventoryRepository(DATABASE_URL)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    repository.init_schema()
    yield


app = FastAPI(title="ShopFlow inventory-svc", lifespan=lifespan)
instrument(app)


def get_repository() -> InventoryRepository:
    return repository


class ReserveIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=1000)


@app.get("/items")
def list_items(repo: InventoryRepository = Depends(get_repository)) -> list[dict]:
    return repo.list()


@app.get("/items/{sku}")
def get_item(sku: str, repo: InventoryRepository = Depends(get_repository)) -> dict:
    item = repo.get(sku)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown SKU")
    return item


@app.post("/reserve")
def reserve(body: ReserveIn, repo: InventoryRepository = Depends(get_repository)) -> dict:
    result, remaining = repo.reserve(body.sku, body.quantity)
    RESERVATIONS_TOTAL.labels(result).inc()

    if result == ReserveResult.UNKNOWN_SKU:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown SKU")
    if result == ReserveResult.OUT_OF_STOCK:
        raise HTTPException(status.HTTP_409_CONFLICT, "Not enough stock")

    log.info("reserved %s x%s (remaining %s)", body.sku, body.quantity, remaining)
    return {"sku": body.sku, "reserved": body.quantity, "remaining": remaining}


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz(response: Response, repo: InventoryRepository = Depends(get_repository)) -> dict:
    try:
        repo.ping()
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "reason": str(exc)}
    return {"status": "ready"}
