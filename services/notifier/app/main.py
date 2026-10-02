"""
notifier: consumes `order.created` events from RabbitMQ and sends the customer a
confirmation (logged here; a real system would call an email/SMS provider).

Runs a tiny HTTP server alongside the consumer for Kubernetes probes and Prometheus.
"""
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pika
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("notifier")

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://shopflow:shopflow@localhost:5672/%2F")
EVENTS_EXCHANGE = os.getenv("EVENTS_EXCHANGE", "shopflow.events")
QUEUE = os.getenv("QUEUE", "notifier.order-created")
HTTP_PORT = int(os.getenv("HTTP_PORT", "8000"))

NOTIFICATIONS_TOTAL = Counter("notifications_total", "Order events handled, by result", ["result"])

# Set while connected to RabbitMQ; drives the readiness probe
connected = threading.Event()


def handle_order_created(body: bytes) -> bool:
    """Process one event. Returns False for messages that can never succeed (poison messages)."""
    try:
        order = json.loads(body)
        order_id, email, sku, quantity = order["id"], order["customer_email"], order["sku"], order["quantity"]
    except (ValueError, KeyError, TypeError) as exc:
        log.error("discarding malformed event: %s", exc)
        NOTIFICATIONS_TOTAL.labels("malformed").inc()
        return False

    log.info("notification sent to %s: order #%s confirmed (%s x%s)", email, order_id, sku, quantity)
    NOTIFICATIONS_TOTAL.labels("sent").inc()
    return True


def _on_message(channel, method, _properties, body):
    """Called by pika for every message that arrives on our queue."""
    if handle_order_created(body):
        # ack = "done, delete it". Until we ack, RabbitMQ keeps the message, so if this
        # pod crashes mid-way the message is redelivered instead of lost.
        channel.basic_ack(delivery_tag=method.delivery_tag)
    else:
        # nack without requeue = "drop it". A malformed message would fail again
        # every time, so requeueing it would loop forever and block the queue.
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def consume_forever() -> None:
    """Connect and consume; if the connection drops, reconnect (no pod restart needed)."""
    while True:
        try:
            connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
            channel = connection.channel()
            # The exchange is where orders-api publishes. "topic" routes by key, e.g. "order.created".
            channel.exchange_declare(exchange=EVENTS_EXCHANGE, exchange_type="topic", durable=True)
            # Our own queue. "durable" means it survives a RabbitMQ restart.
            channel.queue_declare(queue=QUEUE, durable=True)
            # Ask the exchange to copy every "order.created" event into our queue
            channel.queue_bind(queue=QUEUE, exchange=EVENTS_EXCHANGE, routing_key="order.created")
            # Hold at most 10 un-acked messages at a time (back-pressure)
            channel.basic_qos(prefetch_count=10)
            channel.basic_consume(queue=QUEUE, on_message_callback=_on_message)
            connected.set()  # readiness probe now passes
            log.info("consuming %s from exchange %s", QUEUE, EVENTS_EXCHANGE)
            channel.start_consuming()
        except pika.exceptions.AMQPError as exc:
            connected.clear()
            log.warning("rabbitmq connection lost (%s); retrying in 2s", exc)
            time.sleep(2)


class ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (name required by BaseHTTPRequestHandler)
        if self.path == "/healthz":
            self._send(200, b"ok", "text/plain")
        elif self.path == "/readyz":
            ready = connected.is_set()
            self._send(200 if ready else 503, b"ready" if ready else b"not connected", "text/plain")
        elif self.path == "/metrics":
            self._send(200, generate_latest(), CONTENT_TYPE_LATEST)
        else:
            self._send(404, b"not found", "text/plain")

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # keep probe traffic out of the logs
        pass


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), ProbeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("probe/metrics server on :%s", HTTP_PORT)
    consume_forever()


if __name__ == "__main__":
    main()
