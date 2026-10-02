#!/usr/bin/env bash
# End-to-end test against the running kind deployment:
# order -> stock reserved -> order confirmed -> event -> notification sent.
set -euo pipefail

NAMESPACE=shopflow
PORT=18080

kubectl port-forward -n "$NAMESPACE" svc/orders-api "$PORT:8000" >/dev/null 2>&1 &
PF_PID=$!
trap 'kill $PF_PID 2>/dev/null' EXIT
sleep 3

order() {
  curl -s -o /dev/stderr -w "%{http_code}" -X POST "localhost:$PORT/orders" \
    -H "Content-Type: application/json" -d "$1"
}

echo "==> 1. Valid order (expect 201 CONFIRMED)"
code=$(order '{"sku": "SKU-001", "quantity": 2, "customer_email": "jane@example.com"}'); echo
[ "$code" = "201" ] || { echo "FAIL: expected 201, got $code"; exit 1; }

echo "==> 2. More than in stock (expect 409)"
code=$(order '{"sku": "SKU-003", "quantity": 999, "customer_email": "jane@example.com"}'); echo
[ "$code" = "409" ] || { echo "FAIL: expected 409, got $code"; exit 1; }

echo "==> 3. Unknown product (expect 404)"
code=$(order '{"sku": "SKU-404", "quantity": 1, "customer_email": "jane@example.com"}'); echo
[ "$code" = "404" ] || { echo "FAIL: expected 404, got $code"; exit 1; }

echo "==> 4. Notifier received the event"
sleep 2
if kubectl logs -n "$NAMESPACE" deploy/notifier --since=2m | grep -q "notification sent to jane@example.com"; then
  echo "PASS: notification sent"
else
  echo "FAIL: no notification in notifier logs"; exit 1
fi

echo "==> All smoke tests passed"
