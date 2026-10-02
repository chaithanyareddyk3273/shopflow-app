# Container image for inventory-svc.
# Build it with:  docker build -t shopflow/inventory-svc:dev services/inventory-svc

# Start from a small official Python image ("slim" = fewer extra packages = smaller and safer)
FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE: don't write .pyc files (the filesystem is read-only in Kubernetes)
# PYTHONUNBUFFERED:        print logs immediately, so "kubectl logs" shows them in real time
# PIP_*:                   keep pip quiet and don't keep its download cache in the image
ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PIP_NO_CACHE_DIR=1     PIP_DISABLE_PIP_VERSION_CHECK=1

# All following commands run inside /app in the container
WORKDIR /app

# Install dependencies BEFORE copying the code. Docker caches each step, so when only
# the code changes, this slow install step is reused instead of re-run.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the application code
COPY app ./app

# Create an unprivileged user and switch to it. If the app were ever compromised,
# the attacker would not be root. (The Helm chart also enforces runAsNonRoot.)
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
USER 10001

# Document the port the service listens on
EXPOSE 8000

# Start the web server (uvicorn) running the FastAPI app on port 8000, reachable from outside the container (0.0.0.0)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
