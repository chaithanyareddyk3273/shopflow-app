"""Postgres access for orders. Each service owns its own database (database-per-service)."""
import logging
import time

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id             SERIAL PRIMARY KEY,
    sku            TEXT        NOT NULL,
    quantity       INTEGER     NOT NULL CHECK (quantity > 0),
    customer_email TEXT        NOT NULL,
    status         TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

COLUMNS = "id, sku, quantity, customer_email, status, created_at"

# Arbitrary constant: identifies "orders schema setup" among Postgres advisory locks
SCHEMA_LOCK_ID = 727002


class OrderRepository:
    """
    All SQL for orders lives here, so main.py never writes SQL itself.
    (This is the "repository pattern": it also makes the database easy to fake in tests.)
    """

    def __init__(self, dsn: str):
        self.dsn = dsn  # connection string, e.g. postgresql://user:pass@host:5432/orders

    def _connect(self):
        # dict_row: rows come back as dicts ({"id": 1, "sku": ...}) instead of tuples.
        # Used as `with self._connect() as conn:`, the transaction is committed when the
        # block ends without an error, rolled back if one is raised, and the connection closed.
        # Values are always passed as %s parameters, never pasted into the SQL string,
        # which prevents SQL injection.
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=3)

    def init_schema(self, attempts: int = 30, delay: float = 2.0) -> None:
        """Create the table, retrying while Postgres is still starting up."""
        for attempt in range(1, attempts + 1):
            try:
                with self._connect() as conn:
                    # Several replicas start at once. "CREATE TABLE IF NOT EXISTS" is NOT safe
                    # when two connections run it at the same moment (both can try to create,
                    # one fails with a duplicate-key error). This lock makes them take turns;
                    # it's released automatically when the transaction commits.
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
                    conn.execute(SCHEMA)
                log.info("orders schema ready")
                return
            except psycopg.OperationalError as exc:
                log.warning("postgres not ready (attempt %d/%d): %s", attempt, attempts, exc)
                time.sleep(delay)
        raise RuntimeError("postgres unavailable after retries")

    def ping(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1")

    def create(self, sku: str, quantity: int, customer_email: str, status: str) -> dict:
        with self._connect() as conn:
            return conn.execute(
                f"INSERT INTO orders (sku, quantity, customer_email, status) "
                f"VALUES (%s, %s, %s, %s) RETURNING {COLUMNS}",
                (sku, quantity, customer_email, status),
            ).fetchone()

    def set_status(self, order_id: int, status: str) -> dict:
        with self._connect() as conn:
            return conn.execute(
                f"UPDATE orders SET status = %s WHERE id = %s RETURNING {COLUMNS}",
                (status, order_id),
            ).fetchone()

    def get(self, order_id: int) -> dict | None:
        with self._connect() as conn:
            return conn.execute(f"SELECT {COLUMNS} FROM orders WHERE id = %s", (order_id,)).fetchone()

    def list(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            return conn.execute(f"SELECT {COLUMNS} FROM orders ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
