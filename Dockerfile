# Production-oriented image for the optional FastAPI serving layer.
#
# This is a minimal local deployment path (see docs/deployment.md), not a
# distributed platform. It runs the same package installed via `pip install
# -e .` locally -- no Kafka, Redis, or Kubernetes assumptions.

FROM python:3.11-slim AS runtime

# Fail fast on misconfiguration instead of silently starting broken.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PRICING_ENGINE_CONFIG_PATH=/app/configs/base.yaml

WORKDIR /app

# curl is required by the HEALTHCHECK below; kept as the only apt dependency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install only what /v1/pricing (and /run-scenario) need to serve requests.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[api]"

COPY configs/ ./configs/

# Non-root runtime user: least-privilege container execution.
RUN useradd --create-home --shell /usr/sbin/nologin pricing \
    && chown -R pricing:pricing /app
USER pricing

EXPOSE 8000

# Container-level health check backed by the app's own /health endpoint.
# Kubernetes/other orchestrators should still probe /health (liveness) and
# /ready (readiness) directly rather than relying solely on this.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "balance_pricing_engine.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
