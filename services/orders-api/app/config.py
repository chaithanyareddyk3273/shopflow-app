import os

SERVICE_NAME = "orders-api"

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shopflow:shopflow@localhost:5432/orders")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://shopflow:shopflow@localhost:5672/%2F")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8001")

# Events go to a topic exchange so any number of consumers can subscribe
EVENTS_EXCHANGE = os.getenv("EVENTS_EXCHANGE", "shopflow.events")
