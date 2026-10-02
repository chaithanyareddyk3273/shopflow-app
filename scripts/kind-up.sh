#!/usr/bin/env bash
# Build the three images, load them into a local kind cluster and deploy the Helm chart
# with the "local" environment (namespace "shopflow"). This is the fast inner loop for
# trying a change on your laptop; dev and prod are deployed by ArgoCD instead.
# Run from the shopflow-app folder (Git Bash on Windows):   ./scripts/kind-up.sh
set -euo pipefail

CLUSTER=shopflow
NAMESPACE=shopflow
GITOPS_DIR="${GITOPS_DIR:-../shopflow-gitops}"
SERVICES=(orders-api inventory-svc notifier)

for tool in docker kind kubectl helm; do
  command -v "$tool" >/dev/null || { echo "Missing tool: $tool (see README prerequisites)"; exit 1; }
done

if ! kind get clusters | grep -qx "$CLUSTER"; then
  echo "==> Creating kind cluster '$CLUSTER'"
  kind create cluster --config "$GITOPS_DIR/kind/kind-config.yaml"
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

for svc in "${SERVICES[@]}"; do
  echo "==> Building shopflow/$svc:dev"
  docker build -t "shopflow/$svc:dev" "services/$svc"
  kind load docker-image "shopflow/$svc:dev" --name "$CLUSTER"
done

echo "==> Deploying Helm chart"
helm upgrade --install shopflow "$GITOPS_DIR/charts/shopflow" \
  --namespace "$NAMESPACE" --create-namespace \
  -f "$GITOPS_DIR/environments/local/values.yaml"

# The image tag stays "dev", so restart to pick up freshly built images
kubectl rollout restart deployment -n "$NAMESPACE" "${SERVICES[@]}"

echo "==> Waiting for everything to be ready (first run pulls Postgres/RabbitMQ images)"
kubectl rollout status statefulset/postgres statefulset/rabbitmq -n "$NAMESPACE" --timeout=5m
for svc in "${SERVICES[@]}"; do
  kubectl rollout status "deployment/$svc" -n "$NAMESPACE" --timeout=5m
done

kubectl get pods -n "$NAMESPACE"
echo "==> Done. Run ./scripts/smoke-test.sh to test it end to end."
