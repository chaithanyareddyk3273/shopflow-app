"""notifier tests: message handling, ack/nack behaviour and the probe endpoints."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest

from app import main

ORDER_EVENT = {"id": 7, "sku": "SKU-001", "quantity": 2, "customer_email": "jane@example.com", "status": "CONFIRMED"}


def test_valid_event_is_handled():
    assert main.handle_order_created(json.dumps(ORDER_EVENT).encode()) is True


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"id": 1}', b"[]"])
def test_malformed_events_are_rejected(body):
    assert main.handle_order_created(body) is False


def test_valid_message_is_acked():
    channel, method = mock.MagicMock(), mock.MagicMock(delivery_tag=42)
    main._on_message(channel, method, None, json.dumps(ORDER_EVENT).encode())
    channel.basic_ack.assert_called_once_with(delivery_tag=42)
    channel.basic_nack.assert_not_called()


def test_poison_message_is_dropped_not_requeued():
    channel, method = mock.MagicMock(), mock.MagicMock(delivery_tag=43)
    main._on_message(channel, method, None, b"not json")
    channel.basic_nack.assert_called_once_with(delivery_tag=43, requeue=False)


@pytest.fixture
def probe_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), main.ProbeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    main.connected.clear()


def _status(url):
    try:
        return urllib.request.urlopen(url, timeout=2).status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_probes_track_broker_connection(probe_server):
    assert _status(f"{probe_server}/healthz") == 200
    assert _status(f"{probe_server}/readyz") == 503  # not connected yet
    main.connected.set()
    assert _status(f"{probe_server}/readyz") == 200


def test_metrics_endpoint(probe_server):
    main.handle_order_created(json.dumps(ORDER_EVENT).encode())
    body = urllib.request.urlopen(f"{probe_server}/metrics", timeout=2).read().decode()
    assert 'notifications_total{result="sent"}' in body
