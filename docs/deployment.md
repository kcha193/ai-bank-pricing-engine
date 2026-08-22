# Deployment Guide

This project's serving layer (`api/app.py`) is optional and isolated from the core
package — the domain and application layers (`scenarios/`, `application/`, `governance/`,
`features/`, `events/`) have no FastAPI dependency. This guide covers the minimal, secure
local deployment path currently implemented, and lists the production boundaries that are
deliberately **not** implemented yet.

This is a locally runnable prototype deployment path, not a distributed platform. It does
not require Kafka, Redis, Kubernetes, or a cloud provider.

## 1. Local API execution

```bash
python3 -m pip install -e ".[api]"
uvicorn balance_pricing_engine.api.app:app --host 0.0.0.0 --port 8000
```

Verify:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

Endpoints:

- `GET /health` — liveness: process is up. Always returns `200` once the process starts.
- `GET /ready` — readiness: confirms the default engine config
  (`PRICING_ENGINE_CONFIG_PATH`, default `configs/base.yaml`) loads and validates. Returns
  `503` with a structured error body if the config is missing or invalid — treat this as
  "do not route traffic yet," not as a reason to restart the process.
- `POST /run-scenario` — original endpoint, YAML-string request body, unchanged response
  shape. Kept for backward compatibility; new integrations should prefer the versioned
  endpoint below.
- `POST /v1/pricing/scenarios` — versioned, governed endpoint backed by
  `application.PricingService`. Returns decision status, governance gates, model/feature
  lineage, and execution timing. See `api/schemas.py` for the full request/response shape
  and `docs/architecture.md` for the orchestration flow.

### Correlation IDs

Pass a caller-generated ID via the `correlation_id` field in the request body, or the
`X-Correlation-ID` request header. If neither is supplied, the service generates a UUID4.
The correlation ID is always echoed back in the `X-Correlation-ID` response header and in
the response body, and is propagated into structured logs, domain events, and the decision
record.

### Idempotency

`POST /v1/pricing/scenarios` is **not** idempotent. Every call produces a new
`decision_id` (and, if `persist_ledger` is set, a new `run_id` and a new run-ledger
directory) even if the same `correlation_id` is reused. There is no request
deduplication or durable idempotency-key store in this prototype. A production
integration that needs "retry-safe" semantics should add an idempotency-key store
(e.g. keyed on `correlation_id` + config hash) in front of this endpoint — see
*Production boundaries* below.

## 2. Docker execution

Build and run:

```bash
docker build -t ai-bank-pricing-engine .
docker run --rm -p 8000:8000 \
  -e PRICING_ENGINE_CONFIG_PATH=/app/configs/base.yaml \
  ai-bank-pricing-engine
```

The image:

- installs the package with the `api` extra only (no dev/ml/llm dependencies);
- runs as a non-root user (`pricing`);
- exposes port `8000` and declares a container `HEALTHCHECK` against `/health`;
- fails to start serving meaningful traffic if the configured YAML is invalid — `/ready`
  will report `503` immediately, which orchestrators should wire into their own readiness
  gate rather than restarting the container in a crash loop.

To persist run ledgers outside the container, mount a volume and point
`persist_ledger` requests at it via `ledger_base_dir` (currently only configurable through
the application-layer request, not an environment variable — see *Persistence* below):

```bash
docker run --rm -p 8000:8000 \
  -v "$(pwd)/data/outputs/runs:/app/data/outputs/runs" \
  ai-bank-pricing-engine
```

There is no Compose file. A single container with a bind-mounted ledger directory is
sufficient for this prototype's scope; add one only if a second genuinely cooperating
service (e.g. a local event consumer reading the JSONL event log) is introduced.

## 3. Health and readiness behavior

| Signal | Endpoint | Meaning | Orchestrator action |
| --- | --- | --- | --- |
| Liveness | `GET /health` | Process is running | Restart on repeated failure |
| Readiness | `GET /ready` | Default config is loadable | Hold traffic, do not restart |

`/ready` intentionally does not run a full pricing scenario (that would be expensive per
probe); it only re-validates the `EngineConfig` boundary. A scenario-level smoke test is a
reasonable addition to a CI/CD pipeline, not to a hot readiness probe.

## 4. Configuration handling

- `PRICING_ENGINE_CONFIG_PATH` — path to the default config used by `/ready`. Defaults to
  `configs/base.yaml`. `POST /run-scenario` and `POST /v1/pricing/scenarios` both take the
  config as a YAML **string** in the request body — the environment variable only affects
  the readiness probe's default.
- No secrets are read from configuration today; `EngineConfig` contains only synthetic
  pricing assumptions. If a future config field needs a real credential (e.g. a competitor
  rate source requiring an API key), inject it via environment variable or a secrets
  manager — never commit it to `configs/*.yaml`.

## 5. Persistence considerations for run ledgers

Run ledgers (`data/outputs/runs/<timestamp>_<scenario>/`) are written to local disk by
`write_run_ledger()`. In a container, this directory is ephemeral unless mounted as a
volume. For any ledger that must survive container restarts or be shared with a pricing
committee:

- mount `data/outputs/runs` (or a custom `ledger_base_dir`) to durable storage;
- treat the mounted directory as the audit archive described in
  `docs/run_ledger_guide.md` — do not edit files in place after the fact;
- the additive `decision_record.json` (see `docs/architecture.md` and
  `docs/adr/0003-governance-centred-decision-record.md`) is written into the same
  directory and inherits the same persistence requirement.

There is no database-backed ledger store in this prototype; the filesystem directory
*is* the ledger.

## 6. Production boundaries (not implemented)

The following are explicitly out of scope for this prototype and must be addressed before
any real production use:

- **Authentication/authorisation.** **Recommended:** put the service behind an API gateway,
  mTLS, or an auth proxy before exposing it beyond a trusted network — that's still the
  primary control. **Opt-in defense-in-depth** for deployments with no fronting proxy: set
  `PRICING_ENGINE_API_KEYS` (comma-separated for key rotation) and `POST /run-scenario` /
  `POST /v1/pricing/scenarios` require a matching `X-API-Key` header (constant-time
  comparison via `api/auth.py`); `GET /health` and `/ready` stay open, matching standard
  orchestrator-probe convention. Unset (the default), behaviour is unchanged from before.
- **Secrets management.** Two secrets are actively read today through
  `secrets.provider.SecretsProvider` — protocol: `get_secret(name) -> str | None`. Both
  default to `EnvSecretsProvider`, reading environment variables (still not committed to
  source control):
  - `ANTHROPIC_API_KEY` (used by the optional `[llm]` layer) — injected per call via
    `llm.build_client(secrets_provider=...)`/`llm.is_llm_available(secrets_provider=...)`
    (both exported from `balance_pricing_engine.llm`), so a future Vault/AWS Secrets
    Manager/GCP Secret Manager adapter implementing the same protocol can be passed in
    without changing call sites.
  - `PRICING_ENGINE_API_KEYS` (the opt-in API-key auth described above) — resolved through
    the `require_api_key` FastAPI dependency's `_default_secrets_provider`. There's no
    per-call injection point for this one today; swapping it to a different provider means
    overriding `api.auth._default_secrets_provider` (the pattern tests already use via
    `app.dependency_overrides`), not a runtime config flag.

  See `StaticSecretsProvider` for the test/local-dev injection pattern for either secret.
- **TLS.** The container serves plain HTTP by default. **Recommended:** terminate TLS at a
  reverse proxy or load balancer in front of the container — that's where cert rotation via
  ACME/Let's Encrypt and HTTP/2 belong, and it keeps the app process out of the cert-handling
  business. **Direct TLS** (for deployments with no fronting proxy, e.g. a bare container on an
  internal network) is supported: set both `PRICING_ENGINE_TLS_CERTFILE` and
  `PRICING_ENGINE_TLS_KEYFILE` to mounted cert/key paths (`resolve_tls_kwargs` in
  `api/server.py` raises if only one is set, or if either is set but empty — fail fast on
  partial or malformed config rather than silently falling back to plain HTTP) and the
  launcher passes them to uvicorn. To generate a self-signed cert for local testing:

  ```bash
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
    -keyout server.key -out server.crt -subj "/CN=localhost"
  docker run --rm -p 8443:8000 \
    -v "$(pwd)/server.crt:/certs/server.crt:ro" \
    -v "$(pwd)/server.key:/certs/server.key:ro" \
    -e PRICING_ENGINE_TLS_CERTFILE=/certs/server.crt \
    -e PRICING_ENGINE_TLS_KEYFILE=/certs/server.key \
    ai-bank-pricing-engine
  curl -k https://localhost:8443/health
  ```

- **Durable event delivery.** `events/publisher.py` ships `NoOpEventPublisher`,
  `InMemoryEventPublisher`, `JSONLEventPublisher`, and `KafkaEventPublisher` (requires the
  `[kafka]` extra — `pip install 'balance_pricing_engine[kafka]'`). `KafkaEventPublisher`
  blocks on the send future's broker ack and raises on failure or timeout; the first three
  don't survive a process crash between "event constructed" and "event flushed." In
  practice this is still best-effort, not durable at-least-once delivery end-to-end: every
  `PricingService` call routes publishing through `publish_safely()`, which runs the
  publish on its own daemon thread capped at `deadline_seconds` (default 2s) — so a
  broker that stops acknowledging sends can't stall the pricing request past that cap —
  and logs the outcome (including a late failure that finishes after the cap) rather
  than retrying it. A `MAX_CONCURRENT_PUBLISHES`-sized semaphore (default 16) also caps
  how many such threads may be in flight at once, so a sustained outage drops further
  publishes instead of leaking threads without bound. A process crash before the ack
  leaves no recoverable copy either way. A
  durable outbox/retry layer would be needed to close that gap. No live Kafka cluster is
  provisioned by this repo — point `bootstrap_servers`
  at your own broker. A Pub/Sub/Event Hubs adapter implementing the same `EventPublisher`
  protocol is the same documented upgrade path for other clouds
  (`docs/adr/0004-in-process-events-before-streaming-platform.md`).
- **Durable model registry.** `ModelRegistry` is a local JSON file. A multi-instance
  deployment needs a shared, concurrency-safe registry (e.g. MLflow Model Registry or a
  managed equivalent) — see `docs/model_governance.md`.
- **Durable feature store.** `LocalFeatureStore` regenerates synthetic features
  in-process. A real feature store (Feast or a warehouse-backed store) is the documented
  upgrade path (`docs/adr/0002-local-feature-store-interface.md`).
- **Autoscaling.** No horizontal scaling configuration is provided. The service is
  stateless per request (ledger writes are the only local side effect), so it is
  horizontally scalable behind a load balancer once ledger/registry/event stores are
  externalised — until then, running multiple replicas risks registry/ledger write races.
- **Monitoring.** Structured JSON logs (`observability/logging.py`) are the only
  observability signal today. See `docs/architecture.md` for the documented OpenTelemetry
  extension seam.
