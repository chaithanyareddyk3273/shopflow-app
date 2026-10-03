# 📖 Code walkthrough: how ShopFlow works, step by step

This guide explains the code in plain English. **You don't need to know FastAPI, RabbitMQ or Kubernetes to follow it.** New terms are explained the first time they appear, and there's a [glossary](#-glossary) at the end.

**Contents**
1. [The big picture](#1-the-big-picture)
2. [Where everything is](#2-where-everything-is)
3. [Follow one order through the code](#3-follow-one-order-through-the-code)
4. [Every file explained](#4-every-file-explained)
5. [How it runs: Docker, Compose, Kubernetes](#5-how-it-runs-docker-compose-kubernetes)
6. [How the tests work](#6-how-the-tests-work)
7. [Questions to be able to answer](#7-questions-to-be-able-to-answer)
8. [Glossary](#-glossary)

---

## 1. The big picture

ShopFlow is a tiny online shop backend split into **three small programs** (microservices). Each one does one job:

| Service | Its one job | Like a real shop's... |
|---|---|---|
| **orders-api** | Takes customer orders | Checkout counter |
| **inventory-svc** | Knows how much stock there is | Warehouse |
| **notifier** | Tells customers their order is confirmed | Email department |

They talk in two different ways:
- **orders-api → inventory-svc: a direct question (HTTP).** "Can I have 2 keyboards?" It needs the answer *right now*, before it can confirm the order.
- **orders-api → notifier: a message (RabbitMQ).** "Order #7 was confirmed." orders-api doesn't wait for a reply. If the notifier is down, the message waits in a queue until it comes back.

Each service that stores data has **its own database**: `orders` and `inventory`. No service reads another service's database.

---

## 2. Where everything is

```
shopflow-app/
├── services/
│   ├── orders-api/
│   │   ├── app/
│   │   │   ├── main.py              ⭐ START HERE: the API endpoints and the order logic
│   │   │   ├── config.py            settings (database address, etc.) read from environment variables
│   │   │   ├── db.py                all SQL for the orders table
│   │   │   ├── inventory_client.py  calls inventory-svc over HTTP
│   │   │   ├── events.py            publishes messages to RabbitMQ
│   │   │   └── metrics.py           Prometheus metrics (request counts and timings)
│   │   ├── tests/                   automated tests (run with: pytest)
│   │   ├── Dockerfile               how to package this service as a container image
│   │   └── requirements.txt         Python libraries it needs
│   ├── inventory-svc/               same layout: main.py (API) + db.py (SQL)
│   └── notifier/                    main.py only: listens to RabbitMQ
├── scripts/
│   ├── kind-up.sh                   one command: build everything and deploy to local Kubernetes
│   └── smoke-test.sh                one command: test the running system end to end
├── deploy/postgres-init.sql         creates the two databases (used by Docker Compose)
└── docker-compose.yml               run everything without Kubernetes
```

**Suggested reading order:** `orders-api/app/main.py` → `inventory-svc/app/db.py` (the `reserve` method) → `notifier/app/main.py` → `orders-api/tests/test_orders.py`.

---

## 3. Follow one order through the code

A customer sends:

```bash
curl -X POST localhost:8080/orders -H "Content-Type: application/json" \
  -d '{"sku": "SKU-001", "quantity": 2, "customer_email": "jane@example.com"}'
```

Here's exactly what happens, in order.

### Step 1: The request is checked (`orders-api/app/main.py` → `OrderIn`)

```python
class OrderIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=1000)
    customer_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
```

`OrderIn` describes what a valid order looks like. **FastAPI checks every request against it automatically.** If quantity is `0` or the email has no `@`, the customer gets a `422` error and our code never runs. `gt=0` means "greater than 0" and `le=1000` means "less than or equal to 1000".

### Step 2: The order is saved as PENDING (`main.py` → `create_order`, `db.py` → `create`)

```python
order = repo.create(order_in.sku, order_in.quantity, order_in.customer_email, status="PENDING")
```

We save the order **before** checking stock, so every attempt is recorded, even failed ones. `repo.create` in `db.py` runs:

```sql
INSERT INTO orders (sku, quantity, customer_email, status) VALUES (%s, %s, %s, %s) RETURNING ...
```

The `%s` placeholders are filled in safely by the database driver, never pasted into the SQL text. That's what prevents **SQL injection**. `RETURNING` hands back the new row, including its new `id`.

### Step 3: Stock is reserved (`inventory_client.py` → `inventory-svc/app/main.py` → `inventory-svc/app/db.py`)

orders-api calls inventory-svc over HTTP:

```python
inv.reserve(order_in.sku, order_in.quantity)    # POST http://inventory-svc:8000/reserve
```

Inside inventory-svc, **one SQL statement** does the whole reservation:

```sql
UPDATE items SET stock = stock - 2 WHERE sku = 'SKU-001' AND stock >= 2 RETURNING stock
```

The important part is `AND stock >= 2`. The database only subtracts if there's enough. If two customers order the last keyboard at the same moment, the database runs the two updates one after the other, so **one succeeds and the other finds nothing to update**. That's how we prevent overselling without any extra locking code.

If nothing was updated, inventory-svc checks *why* (unknown product, or not enough stock) and answers:

| inventory-svc answers | `inventory_client.py` raises | orders-api returns | order status becomes |
|---|---|---|---|
| `200` reserved | *(nothing, success)* | continues to step 4 | CONFIRMED |
| `409` not enough stock | `OutOfStock` | `409` | REJECTED |
| `404` unknown SKU | `UnknownSku` | `404` | REJECTED |
| no answer / timeout / error | `InventoryUnavailable` | `503` | FAILED |

### Step 4: The order is confirmed (`main.py` → `_finish`)

```python
order = _finish(repo, order["id"], "CONFIRMED")
```

`_finish` updates the status in the database **and** adds 1 to the `orders_total{status="CONFIRMED"}` metric.

### Step 5: An event is published (`events.py`)

```python
events.publish("order.created", order)
```

This sends the order as JSON to RabbitMQ's **exchange** `shopflow.events` with the label (routing key) `order.created`. Two settings make this reliable:
- `confirm_delivery()` plus `mandatory=True`: RabbitMQ must confirm it received the message, and if no queue is listening for `order.created`, it raises an error instead of silently throwing the message away. (This was a real bug; see "What broke and how I fixed it" in the README.)
- `delivery_mode=2`: the message is saved to disk, so it survives a RabbitMQ restart.

If publishing fails, the order **still succeeds**, because stock is already reserved. We log the error and add 1 to `order_event_publish_failures_total`, so it shows up in monitoring.

### Step 6: The customer is notified (`notifier/app/main.py`)

The notifier has been listening the whole time. At startup, `consume_forever()` set up:

```python
channel.queue_declare(queue="notifier.order-created", durable=True)           # our own mailbox
channel.queue_bind(queue=..., exchange="shopflow.events", routing_key="order.created")  # "copy order.created events into it"
```

When the event arrives, `_on_message` → `handle_order_created` reads the JSON and "sends" the notification (here it writes a log line; a real system would call an email service). Then:
- **Success → `basic_ack`:** "done, you can delete it." If the notifier crashes *before* acking, RabbitMQ re-delivers the message, so nothing is lost.
- **Broken message → `basic_nack(requeue=False)`:** "drop it." Re-delivering a broken message would just fail again forever.

### Step 7: The customer gets the response

orders-api returns `201 Created` with the saved order:

```json
{"id": 7, "sku": "SKU-001", "quantity": 2, "customer_email": "jane@example.com", "status": "CONFIRMED", "created_at": "..."}
```

---

## 4. Every file explained

### orders-api
| File | What's in it |
|---|---|
| `app/main.py` | The **endpoints** (`POST /orders`, `GET /orders`, `GET /orders/{id}`, `/healthz`, `/readyz`) and the order logic from section 3 |
| `app/config.py` | Settings from **environment variables** (`DATABASE_URL`, `RABBITMQ_URL`, `INVENTORY_URL`). The same image runs everywhere; only these change. `FAULT_INJECTION_RATE` (off by default) makes a share of orders fail on purpose, to test that a bad canary release is rolled back. |
| `app/db.py` | `OrderRepository`: every SQL statement for the `orders` table. `init_schema` creates the table at startup, and **keeps retrying** (30 tries, 2 seconds apart) while Postgres is still starting. |
| `app/inventory_client.py` | Calls inventory-svc and turns HTTP status codes into Python exceptions. Always uses a **timeout**. |
| `app/events.py` | `EventPublisher`: sends events to RabbitMQ with delivery confirmation |
| `app/metrics.py` | Records how long each request takes, plus the `/metrics` endpoint that Prometheus reads |

### inventory-svc
| File | What's in it |
|---|---|
| `app/main.py` | Endpoints: `GET /items`, `GET /items/{sku}`, `POST /reserve`, plus health checks |
| `app/db.py` | `InventoryRepository`: creates the `items` table, adds 3 demo products, and does the **atomic reservation** from step 3 |

### notifier
| File | What's in it |
|---|---|
| `app/main.py` | `consume_forever` (connects to RabbitMQ and **reconnects automatically** if the connection drops), `handle_order_created` (processes one event), and `ProbeHandler`, a tiny web server for `/healthz`, `/readyz` and `/metrics` |

### Health checks: why there are two
| Endpoint | Question it answers | If it fails, Kubernetes... |
|---|---|---|
| `/healthz` (**liveness**) | "Is the program alive?" | **restarts** the pod |
| `/readyz` (**readiness**) | "Can it do work right now?" (database or RabbitMQ reachable) | **stops sending it traffic** until it recovers, without restarting it |

`/healthz` deliberately does *not* check the database. If Postgres goes down, restarting every pod wouldn't fix anything and would only cause a "restart storm".

---

## 5. How it runs: Docker, Compose, Kubernetes

### `Dockerfile`: packaging a service
Every line is commented in the file itself. In short: start from a small Python image → install the libraries → copy the code → **switch to a non-root user** → start the server. The libraries are installed *before* the code is copied because Docker caches each step, so changing code doesn't re-download every library.

### `docker-compose.yml`: everything on one laptop, no Kubernetes
Starts 5 containers: Postgres, RabbitMQ and the 3 services. `depends_on ... condition: service_healthy` makes each service wait until the database and RabbitMQ are actually ready.

### `scripts/kind-up.sh`: real Kubernetes on your laptop
1. Creates a local Kubernetes cluster with **kind** (Kubernetes running inside Docker), if one doesn't exist
2. Builds the 3 images and loads them into the cluster
3. Installs the **Helm chart** from the `shopflow-gitops` repo (the Kubernetes setup, explained in that repo's walkthrough)
4. Restarts the services to pick up the new images, then waits until every pod is ready

### `scripts/smoke-test.sh`: proof the whole system works
Sends 3 real orders (a valid one, one with too much quantity, and one with an unknown product), checks each response code (201, 409, 404), then checks the notifier's log to confirm the notification was sent. Pass a namespace to test another environment: `./scripts/smoke-test.sh shopflow-dev`.

### `.github/workflows/ci.yml`: the automatic pipeline
GitHub runs this file on every push and pull request. It has 3 jobs, and each one only starts if the previous one passed:

| Job | In plain English |
|---|---|
| **test** | For each of the 3 services, at the same time: install the libraries, run the linter (`ruff`, which catches mistakes like unused imports), run the unit tests (`pytest`) |
| **build** | Build each service's Docker image, then scan it with **Trivy**, a security scanner that knows every published vulnerability. If it finds a serious one that has a fix, the pipeline **stops**. On `main` only, it then uploads the image to GitHub Container Registry with the tag `sha-<commit>`. |
| **deploy-dev** | On `main` only: edit `environments/dev/values.yaml` in the **shopflow-gitops** repo to use the new tag, and commit it. ArgoCD (in the cluster) sees the commit and deploys it. |

Notice that **CI never runs `kubectl`**: it has no access to the cluster at all. It only changes Git; ArgoCD does the deploying. The [gitops walkthrough](https://github.com/chaithanyareddyk3273/shopflow-gitops/blob/main/docs/HELM_WALKTHROUGH.md#9-argocd-and-the-pipeline) explains the rest of the journey to prod.

---

## 6. How the tests work

The unit tests **don't need Postgres, RabbitMQ or the other services**. They use **fakes**: small in-memory stand-ins.

```python
class FakeInventory:
    def __init__(self, error=None):
        self.error = error
    def reserve(self, sku, quantity):
        if self.error:
            raise self.error        # pretend inventory-svc said "out of stock", etc.
```

Then each test swaps the real object for the fake:

```python
app.dependency_overrides = {get_inventory: lambda: fake_inventory, ...}
```

This works because endpoints get their dependencies through `Depends(get_inventory)` instead of using a global directly, so a test can replace them. One test can then say *"inventory is out of stock → expect 409 and status REJECTED"* and run in milliseconds.

```bash
cd services/orders-api
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
```

---

## 7. Questions to be able to answer

<details><summary><b>Why separate services instead of one app?</b></summary>
Each can be deployed, scaled and fixed independently. If the notifier crashes, customers can still place orders. The trade-off is more moving parts: network calls can fail, and you need monitoring across services.
</details>

<details><summary><b>Why HTTP to inventory, but RabbitMQ to the notifier?</b></summary>
The order needs the stock answer immediately to decide CONFIRMED or REJECTED, so that's a synchronous call. The notification can happen a second later, so it's asynchronous: orders don't wait for it, and don't fail if the notifier is down.
</details>

<details><summary><b>How do you stop two customers buying the last item?</b></summary>
One atomic SQL statement: <code>UPDATE ... SET stock = stock - n WHERE sku = ... AND stock >= n</code>. The database processes concurrent updates to the same row one after the other, so only one can succeed when stock runs out.
</details>

<details><summary><b>What happens if RabbitMQ is down when an order is placed?</b></summary>
The order is still confirmed (stock is reserved). The publish fails, which is logged and counted in <code>order_event_publish_failures_total</code>, so the failure is visible. The event isn't retried yet; the fix is the <b>transactional outbox pattern</b>: save the event in the same database transaction as the order, and have a separate process send it.
</details>

<details><summary><b>Two replicas start at the same time. What can go wrong with the database?</b></summary>
Both run their schema setup at once. <code>CREATE TABLE IF NOT EXISTS</code> isn't safe across concurrent connections: both can decide the table is missing, and one crashes with a duplicate-key error. This really happened once autoscaling was on (see "Bug 2" in the README). The fix is a Postgres <b>advisory lock</b> (<code>pg_advisory_xact_lock</code>) in the same transaction, so replicas take turns. In bigger systems, migrations run as a separate one-off job before the new version starts.
</details>

<details><summary><b>Liveness vs readiness?</b></summary>
Liveness failing means "restart me". Readiness failing means "don't send me traffic yet". The database check goes in readiness only, so a database outage doesn't trigger pointless restarts.
</details>

<details><summary><b>Tell me about a bug you fixed.</b></summary>
See "What broke and how I fixed it" in the README: events were silently dropped at startup because RabbitMQ reported healthy before its port was open. Fixed with publisher confirms + mandatory routing, a better healthcheck, and faster reconnects, then verified with live tests.
</details>

---

## 📚 Glossary

| Term | Meaning |
|---|---|
| **Microservice** | A small program that does one job and talks to others over the network |
| **API / endpoint** | A URL a program can call, e.g. `POST /orders` |
| **HTTP status code** | The number in a response: `200` OK, `201` created, `404` not found, `409` conflict, `422` invalid input, `503` temporarily unavailable |
| **FastAPI** | The Python web framework used for orders-api and inventory-svc |
| **Pydantic** | Checks that incoming JSON has the right fields and types |
| **SKU** | Stock Keeping Unit: a product code, like `SKU-001` |
| **RabbitMQ** | A message broker: services drop messages in, other services pick them up later |
| **Exchange / queue / binding** | The exchange receives messages; a queue stores them for one consumer; a binding says which messages go into which queue |
| **ack / nack** | A consumer saying "done, delete it" or "failed" |
| **Container / image** | An image is a packaged app plus everything it needs; a container is a running copy of it |
| **Kubernetes** | Runs containers across machines, restarts them when they fail, and balances traffic |
| **Pod** | The smallest unit Kubernetes runs: one (or a few) containers |
| **Deployment** | Tells Kubernetes "keep N copies of this pod running" |
| **Service (Kubernetes)** | A stable name and address for a group of pods, e.g. `http://inventory-svc:8000` |
| **StatefulSet** | Like a Deployment, but for things with data (Postgres, RabbitMQ), with stable names and storage |
| **Helm / chart** | A templating tool for Kubernetes files; a chart is a package of those templates |
| **kind** | "Kubernetes in Docker": a real Kubernetes cluster on your laptop |
| **Prometheus / metrics** | Prometheus collects numbers (metrics) like request counts from each service's `/metrics` page |
| **Environment variable** | A setting given to a program from outside, e.g. `DATABASE_URL` |
