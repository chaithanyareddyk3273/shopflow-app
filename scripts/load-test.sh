#!/usr/bin/env bash
# Send a steady stream of orders to orders-api from inside the cluster, to watch the
# autoscaler add pods, fill the Grafana dashboard, or give a canary real traffic.
#
#   ./scripts/load-test.sh start shopflow-dev [workers]   # default 3 workers
#   ./scripts/load-test.sh stop  shopflow-dev
#
# Each worker is a tiny busybox pod posting an order roughly every 50 ms.
set -euo pipefail

ACTION="${1:?usage: $0 start|stop <namespace> [workers]}"
NAMESPACE="${2:?usage: $0 start|stop <namespace> [workers]}"
WORKERS="${3:-3}"

case "$ACTION" in
  start)
    kubectl apply -n "$NAMESPACE" -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: load-generator
  labels: { app: load-generator }
spec:
  replicas: $WORKERS
  selector:
    matchLabels: { app: load-generator }
  template:
    metadata:
      labels: { app: load-generator }
    spec:
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: load
          image: busybox:1.36
          command: ["sh", "-c"]
          args:
            - |
              i=0
              while true; do
                i=\$((i+1))
                sku="SKU-00\$(( i % 3 + 1 ))"
                wget -q -O /dev/null --header "Content-Type: application/json" \
                  --post-data "{\"sku\": \"\$sku\", \"quantity\": 1, \"customer_email\": \"load\$i@example.com\"}" \
                  http://orders-api:8000/orders 2>/dev/null || true
                sleep 0.05
              done
          resources:
            requests: { cpu: 10m, memory: 16Mi }
            limits: { memory: 32Mi }
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
EOF
    echo "Load running in $NAMESPACE with $WORKERS workers. Watch: kubectl get hpa -n $NAMESPACE -w"
    ;;
  stop)
    kubectl delete deployment load-generator -n "$NAMESPACE" --ignore-not-found
    ;;
  *)
    echo "usage: $0 start|stop <namespace> [workers]"; exit 1 ;;
esac
