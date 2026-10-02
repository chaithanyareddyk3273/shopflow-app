#!/usr/bin/env bash
# End-to-end test against the running kind deployment:
# order -> stock reserved -> order confirmed -> event -> notification sent.
# Usage: ./scripts/smoke-test.sh [namespace]   (default: shopflow; also shopflow-dev, shopflow-prod)
set -euo pipefail

NAMESPACE="${1:-shopflow}"
PORT=18080

BODY=$(mktemp)
kubectl port-forward -n "$NAMESPACE" svc/orders-api "$PORT:8000" >/dev/null 2>&1 &
PF_PID=$!
trap 'kill $PF_PID 2>/dev/null; rm -f "$BODY"' EXIT
sleep 3

# Prints the response body to stderr and echoes the HTTP status code.
# (Writes via a temp file: curl can't write to /dev/stderr in Git Bash on Windows.)
order() {
  local code
  code=$(curl -s -o "$BODY" -w "%{http_code}" -X POST "localhost:$PORT/orders" \
    -H "Content-Type: application/json" -d "$1")
  cat "$BODY" >&2
  echo "$code"
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
