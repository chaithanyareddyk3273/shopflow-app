"""Postgres access for inventory. Owns the `inventory` database (database-per-service)."""
import logging
import time

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    sku   TEXT    PRIMARY KEY,
    name  TEXT    NOT NULL,
    stock INTEGER NOT NULL CHECK (stock >= 0)
)
"""

# Arbitrary constant: identifies "inventory schema setup" among Postgres advisory locks
SCHEMA_LOCK_ID = 727001

SEED_ITEMS = [
    ("SKU-001", "Mechanical Keyboard", 50),
    ("SKU-002", "USB-C Hub", 100),
    ("SKU-003", "4K Monitor", 10),
]


class ReserveResult:
    RESERVED = "reserved"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN_SKU = "unknown_sku"


class InventoryRepository:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def _connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=3)

    def init_schema(self, attempts: int = 30, delay: float = 2.0) -> None:
        """Create and seed the table, retrying while Postgres is still starting up."""
        for attempt in range(1, attempts + 1):
            try:
                with self._connect() as conn:
                    # Several replicas start at once. "CREATE TABLE IF NOT EXISTS" is NOT safe
                    # when two connections run it at the same moment (both can try to create,
                    # one fails with a duplicate-key error). This lock makes them take turns;
                    # it's released automatically when the transaction commits.
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
                    conn.execute(SCHEMA)
                    with conn.cursor() as cur:
                        cur.executemany(
                            "INSERT INTO items (sku, name, stock) VALUES (%s, %s, %s) ON CONFLICT (sku) DO NOTHING",
                            SEED_ITEMS,
                        )
                log.info("inventory schema ready")
                return
            except psycopg.OperationalError as exc:
                log.warning("postgres not ready (attempt %d/%d): %s", attempt, attempts, exc)
                time.sleep(delay)
        raise RuntimeError("postgres unavailable after retries")

    def ping(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1")

    def list(self) -> list[dict]:
        with self._connect() as conn:
            return conn.execute("SELECT sku, name, stock FROM items ORDER BY sku").fetchall()

    def get(self, sku: str) -> dict | None:
        with self._connect() as conn:
            return conn.execute("SELECT sku, name, stock FROM items WHERE sku = %s", (sku,)).fetchone()

    def reserve(self, sku: str, quantity: int) -> tuple[str, int | None]:
        """
        Atomically decrement stock. The `stock >= quantity` guard in a single UPDATE
        means two concurrent orders can never oversell the last units.
        Returns (result, remaining_stock).
        """
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE items SET stock = stock - %s WHERE sku = %s AND stock >= %s RETURNING stock",
                (quantity, sku, quantity),
            ).fetchone()
            if row is not None:
                return ReserveResult.RESERVED, row["stock"]
            exists = conn.execute("SELECT 1 FROM items WHERE sku = %s", (sku,)).fetchone()
            return (ReserveResult.OUT_OF_STOCK if exists else ReserveResult.UNKNOWN_SKU), None
