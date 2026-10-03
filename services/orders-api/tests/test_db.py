"""Schema setup must be safe when several replicas start at the same time (psycopg mocked)."""
from unittest import mock

from app.db import SCHEMA_LOCK_ID, OrderRepository


def test_schema_setup_takes_advisory_lock_before_creating_table():
    with mock.patch("app.db.psycopg.connect") as connect:
        conn = connect.return_value.__enter__.return_value

        OrderRepository("postgresql://test").init_schema()

    statements = [call.args[0] for call in conn.execute.call_args_list]
    assert statements[0] == "SELECT pg_advisory_xact_lock(%s)"
    assert conn.execute.call_args_list[0].args[1] == (SCHEMA_LOCK_ID,)
    assert "CREATE TABLE IF NOT EXISTS orders" in statements[1]
