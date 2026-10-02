"""Publishes domain events (e.g. order.created) to RabbitMQ."""
import json

import pika


class EventPublisher:
    def __init__(self, url: str, exchange: str):
        self.url = url
        self.exchange = exchange

    def publish(self, routing_key: str, payload: dict) -> None:
        # A short-lived connection per event keeps this simple and robust to broker
        # restarts; a long-lived channel would be the next step under real load.
        connection = pika.BlockingConnection(pika.URLParameters(self.url))
        try:
            channel = connection.channel()
            channel.exchange_declare(exchange=self.exchange, exchange_type="topic", durable=True)
            channel.basic_publish(
                exchange=self.exchange,
                routing_key=routing_key,
                body=json.dumps(payload, default=str),
                properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
            )
        finally:
            connection.close()
