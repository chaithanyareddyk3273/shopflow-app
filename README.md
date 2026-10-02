# 🛒 ShopFlow: Microservices on Kubernetes with CI/CD and GitOps

[![CI](https://github.com/chaithanyareddyk3273/shopflow-app/actions/workflows/ci.yml/badge.svg)](https://github.com/chaithanyareddyk3273/shopflow-app/actions/workflows/ci.yml)
![Kubernetes](https://img.shields.io/badge/Kubernetes-Helm%20%7C%20kind-326CE5?logo=kubernetes&logoColor=white)
![ArgoCD](https://img.shields.io/badge/GitOps-ArgoCD-EF7B4D?logo=argo&logoColor=white)
![Trivy](https://img.shields.io/badge/Security-Trivy-1904DA?logo=aqua&logoColor=white)

ShopFlow is a small order-processing system built as **three Python microservices**, deployed to Kubernetes with Helm. It's built in phases toward a full **CI/CD + GitOps (ArgoCD)** platform on **Amazon EKS**.

> 📖 **New here? Start with the [code walkthrough](docs/CODE_WALKTHROUGH.md).** It follows one order through the code step by step, in plain English.
>
> Kubernetes manifests live in the companion repo **[shopflow-gitops](https://github.com/chaithanyareddyk3273/shopflow-gitops)**, following the GitOps convention of keeping app code and deployment config separate.

---

## 🏗️ Architecture

```mermaid
flowchart LR
    C([Client]) -->|POST /orders| O[orders-api<br/>FastAPI]
    O -->|POST /reserve<br/>sync HTTP| I[inventory-svc<br/>FastAPI]
    O --> PGO[(Postgres<br/>orders DB)]
    I --> PGI[(Postgres<br/>inventory DB)]
    O -->|order.created<br/>async event| MQ{{RabbitMQ<br/>topic exchange}}
    MQ --> N[notifier<br/>worker]
    N -->|confirmation| E([Customer email])
```

| Service | Responsibility | Talks to |
|---|---|---|
| **orders-api** | Accepts orders and tracks their status (`PENDING → CONFIRMED / REJECTED / FAILED`) | inventory-svc (HTTP), Postgres, RabbitMQ |
| **inventory-svc** | Owns product stock and reserves it atomically | Postgres |
| **notifier** | Consumes `order.created` events and notifies the customer | RabbitMQ |

**Order flow:** orders-api saves the order as `PENDING` → reserves stock in inventory-svc → marks it `CONFIRMED` → publishes `order.created` → notifier sends the confirmation.

---

## 🔄 CI/CD and GitOps

```mermaid
flowchart LR
    PUSH([git push]) --> T[Test<br/>ruff + pytest<br/>×3 services]
    T --> B[Build image<br/>+ Trivy scan]
    B -->|main only| R[(GHCR<br/>sha-abc1234)]
    R --> G[Commit new tag to<br/>shopflow-gitops<br/>environments/dev]
    G -->|ArgoCD auto-sync| DEV[shopflow-dev]
    DEV -.->|Promote workflow<br/>opens a PR| PR{{PR: dev tags<br/>→ prod}}
    PR -->|human merges| PROD[shopflow-prod]
```

| Stage | What happens | Where |
|---|---|---|
| **Test** | Lint (ruff) and unit tests, run in parallel for each service | [`ci.yml`](.github/workflows/ci.yml), every push and PR |
| **Build + scan** | Builds each image and scans it with **Trivy**. HIGH/CRITICAL vulnerabilities with an available fix **fail the build**, so a vulnerable image is never published. | every push and PR |
| **Publish** | Pushes images to GitHub Container Registry, tagged `sha-<commit>` (immutable) | `main` only |
| **Deploy to dev** | Commits the new tag to [`shopflow-gitops/environments/dev`](https://github.com/chaithanyareddyk3273/shopflow-gitops/tree/main/environments/dev). **ArgoCD** sees the commit and deploys it. | `main` only |
| **Promote to prod** | A workflow in shopflow-gitops opens a **pull request** copying dev's tags into prod. Merging it deploys prod; reverting it rolls back. | manual, reviewed |

**CI never touches the cluster.** It has no Kubernetes credentials at all; it only changes Git. ArgoCD, running *inside* the cluster, pulls the change. Every deployment is a Git commit, so it's reviewable and auditable, and a rollback is `git revert`.

---

## 🧠 Design decisions

| Decision | Why |
|---|---|
| **Database per service** | Each service owns its data; inventory-svc is the only writer of stock. Services share one Postgres *server* for cost, but use separate *databases*. |
| **Sync HTTP for stock, async events for notifications** | The order must know immediately whether stock exists, but a notification can arrive later. A notifier outage never blocks orders. |
| **Atomic stock reservation** | `UPDATE … SET stock = stock - n WHERE sku = … AND stock >= n` is a single statement, so concurrent orders can't oversell the last item. No application-level locks. |
| **Persist the order before reserving** | Every attempt is auditable, including rejected and failed ones. |
| **Publisher confirms + mandatory routing** | RabbitMQ must acknowledge every event, and an event with no bound queue raises an error instead of vanishing silently (see below). |
| **Poison messages are dropped, not requeued** | A malformed event would otherwise be redelivered forever and block the queue. |
| **Separate liveness and readiness** | `/healthz` (liveness) never checks dependencies, so a database outage doesn't trigger a restart storm. `/readyz` (readiness) checks them, removing the pod from traffic until they recover. |
| **Memory limits, no CPU limits** | CPU requests are enough for scheduling; CPU limits cause throttling and latency spikes. |
| **Hardened pods** | Non-root user, read-only root filesystem, all Linux capabilities dropped, seccomp `RuntimeDefault`, no service-account token. |
| **Immutable image tags** | Deployments reference `sha-<commit>`, never `latest`, so every environment states exactly which code it runs, and a rollback is reproducible. |
| **Pull-based GitOps** | CI has no cluster credentials; ArgoCD pulls from Git. A leaked CI secret can't touch the cluster, and manual `kubectl` changes are reverted by ArgoCD's self-heal. |
| **Security patches at build time** | Dockerfiles run `apt-get upgrade`, because the official Python image can lag behind Debian's security fixes (see below). |
| **Third-party actions pinned to commits** | The Trivy action is pinned to a commit SHA, not a tag, because a tag can be moved to different code. |

---

## 🐛 What broke and how I fixed it

**Symptom:** on a fresh `docker compose up`, the first order was confirmed but the customer never got a notification, and nothing reported a problem.

**Root cause:** two things combined.
1. RabbitMQ's healthcheck (`rabbitmq-diagnostics ping`) passed *before* the AMQP port accepted connections. The notifier started, was refused, and backed off for 5 seconds before retrying.
2. In that window orders-api published `order.created`. A topic exchange with **no queue bound to it silently drops messages**, and the notifier hadn't declared its queue yet. The event was lost, and the publish call still returned success.

**Fix:**
- **orders-api** now uses **publisher confirms** with `mandatory=True`. An unroutable event raises `UnroutableError`, which is logged and counted in `order_event_publish_failures_total`. Lost events are now visible and can be alerted on. Covered by `tests/test_events.py`.
- **Compose** waits on `check_port_connectivity` instead of `ping`. (The Kubernetes readiness probe already did.)
- **notifier** retries every 2 seconds instead of 5.

**Verified** by starting orders-api without the notifier: the order returns `201`, the log shows `UnroutableError`, and the metric reads `1`. A full fresh start now delivers the notification. **Still open:** detection isn't the same as delivery. Guaranteeing delivery needs the transactional outbox pattern (see the roadmap).

### Security: what the first Trivy scan found
Before turning on the "fail on HIGH/CRITICAL" gate, I scanned the existing images locally:

| Finding | Where | Fix |
|---|---|---|
| 3 HIGH CVEs in **starlette 0.41.3**, the web layer under FastAPI (e.g. CVE-2025-62727) | orders-api, inventory-svc | Upgraded FastAPI 0.115 → 0.142 and pinned starlette 1.7.0 |
| 1 HIGH CVE in **libpcre2** (CVE-2026-103111): Debian had released a fix, but the official `python:3.12-slim` image hadn't picked it up yet | all 3 images | `apt-get upgrade` during the build |

Result: **0 HIGH/CRITICAL** in all three images, all tests passing, and the end-to-end test passing on kind. The gate in CI keeps it that way.

---

## 📈 Observability

Every service exposes Prometheus metrics on `/metrics`:

| Metric | Meaning |
|---|---|
| `http_request_duration_seconds{method,route,status}` | Request rate, errors and latency per route (RED method) |
| `orders_total{status}` | Orders by final status |
| `order_event_publish_failures_total` | Confirmed orders whose event failed to publish |
| `inventory_reservations_total{result}` | Reservations: `reserved` / `out_of_stock` / `unknown_sku` |
| `notifications_total{result}` | Notifications `sent` / `malformed` |

Prometheus and Grafana dashboards arrive in Phase 3.

---

## 🚀 Run it locally

**Prerequisites:** [Docker Desktop](https://www.docker.com/products/docker-desktop/), [kind](https://kind.sigs.k8s.io/), [kubectl](https://kubernetes.io/docs/tasks/tools/), [Helm](https://helm.sh/). Clone both repos side by side:

```
projects/
├── shopflow-app/
└── shopflow-gitops/
```

### Option A: Kubernetes (kind), quick local loop
```bash
cd shopflow-app
./scripts/kind-up.sh      # creates the cluster, builds + loads images, installs the chart ("local" environment)
./scripts/smoke-test.sh   # end-to-end test: order → stock → event → notification
```

### Option B: the full GitOps setup (ArgoCD deploys dev and prod from Git)
```bash
cd shopflow-gitops
./scripts/argocd-up.sh                            # installs ArgoCD and the app-of-apps
cd ../shopflow-app
./scripts/smoke-test.sh shopflow-dev              # test what ArgoCD deployed to dev
./scripts/smoke-test.sh shopflow-prod             # ...and to prod
```

**One-time setup for the pipeline** (needed only if you fork the repos): create a fine-grained GitHub token with **Contents: read and write** and **Pull requests: read and write** on `shopflow-gitops` only, and add it as a repository secret named **`GITOPS_TOKEN`** in both repos. CI uses it to commit new image tags to dev and to open promotion PRs.

### Option C: Docker Compose (no Kubernetes)
```bash
docker compose up --build
curl -X POST localhost:8080/orders -H "Content-Type: application/json" \
  -d '{"sku": "SKU-001", "quantity": 2, "customer_email": "jane@example.com"}'
```

Seeded products: `SKU-001` (Mechanical Keyboard, 50), `SKU-002` (USB-C Hub, 100), `SKU-003` (4K Monitor, 10).

### Unit tests
Each service has its own tests using in-memory fakes, so no database or broker is needed:
```bash
cd services/orders-api
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

---

## 📁 Repository layout

```
shopflow-app/
├── .github/workflows/ci.yml   # CI/CD: test → build + scan → publish → deploy to dev
├── services/
│   ├── orders-api/        # FastAPI · Postgres · RabbitMQ publisher
│   ├── inventory-svc/     # FastAPI · Postgres
│   └── notifier/          # RabbitMQ consumer + probe/metrics server
│       ├── app/
│       ├── tests/
│       ├── Dockerfile     # python:3.12-slim, non-root
│       └── requirements.txt
├── scripts/
│   ├── kind-up.sh         # local Kubernetes deploy ("local" environment)
│   └── smoke-test.sh      # end-to-end test: ./scripts/smoke-test.sh [namespace]
├── docs/CODE_WALKTHROUGH.md  # plain-English guide: one order through the code
├── deploy/postgres-init.sql
└── docker-compose.yml
```

---

## 🗺️ Roadmap

- [x] **Phase 1: Microservices on Kubernetes.** 3 services, Dockerfiles, Helm chart, kind, tests
- [x] **Phase 2: CI/CD + GitOps.** GitHub Actions (test → build → Trivy scan → push to GHCR), ArgoCD app-of-apps, dev and prod environments, promotion by pull request. First run: all jobs green, ArgoCD deployed dev automatically, prod deployed by merging [promotion PR #1](https://github.com/chaithanyareddyk3273/shopflow-gitops/pull/1), and the end-to-end test passed in both.
- [ ] **Phase 3: Production on AWS.** Terraform EKS, Prometheus + Grafana, HPA, NetworkPolicies, Sealed Secrets, Argo Rollouts canary deployments

**Known trade-offs to address:** if the database write fails after stock is reserved, that stock isn't released. The fix is a compensating action or the saga pattern. Similarly, if RabbitMQ is unavailable or the event can't be routed at publish time, the order stays confirmed but the event is not retried. The failure is logged and counted in `order_event_publish_failures_total`. The fix is the transactional outbox pattern. Both are planned for later phases.
