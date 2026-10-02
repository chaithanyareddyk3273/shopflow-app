"""
Settings for orders-api, read from environment variables.

The same container image runs everywhere (laptop, kind, EKS); only these variables
change. The defaults below are for running the code directly on a laptop. In
Kubernetes, the Helm chart sets the real values.
"""
import os

SERVICE_NAME = "orders-api"

# Where this service's own database lives (each service has its own database)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shopflow:shopflow@localhost:5432/orders")
# RabbitMQ connection. "%2F" is "/", the default virtual host, URL-encoded.
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://shopflow:shopflow@localhost:5672/%2F")
# Base URL of inventory-svc. In Kubernetes this is its Service name: http://inventory-svc:8000
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8001")

# Events go to a "topic" exchange so any number of services can subscribe to them
EVENTS_EXCHANGE = os.getenv("EVENTS_EXCHANGE", "shopflow.events")
