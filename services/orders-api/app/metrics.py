"""Prometheus metrics: request rate, errors and latency per route (the RED method)."""
import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Histogram, generate_latest

REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route", "status"],
)


def instrument(app: FastAPI) -> None:
    @app.middleware("http")
    async def record_metrics(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        # Use the route template (/orders/{order_id}) so metric labels stay low-cardinality
        path = route.path if route else "unmatched"
        if path != "/metrics":
            REQUEST_LATENCY.labels(request.method, path, str(response.status_code)).observe(
                time.perf_counter() - start
            )
        return response

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
