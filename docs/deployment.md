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
python3 -m balance_pricing_engine.api.server
```

The supported launcher binds `127.0.0.1:8000`. It validates authentication, resource
limits and TLS configuration **before opening a socket**. Do not launch the application
with a raw `uvicorn ...` command or replace the container command: those unsupported
paths bypass the launcher's binding and concurrency policy.

For authenticated local use, set a generated key before starting the same launcher:

```bash
export PRICING_ENGINE_API_KEYS="$(openssl rand -hex 32)"
python3 -m balance_pricing_engine.api.server
```

Both pricing POST endpoints then require `X-API-Key`. Comma-separated keys support
rotation; spaces around entries are allowed. Empty entries, embedded whitespace/control
characters and non-ASCII keys fail closed. Never put a real key in a committed file.

`PRICING_ENGINE_API_HOST` explicitly selects another binding. Every non-numeric-loopback
address, including wildcard addresses and hostnames, requires valid configured keys.
There is no unauthenticated external-binding override. A key is required even behind a
proxy. Reviewed network access controls and TLS remain necessary for remote use.

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
export PRICING_ENGINE_API_KEYS="$(openssl rand -hex 32)"
docker run --rm -p 127.0.0.1:8000:8000 \
  -e PRICING_ENGINE_API_KEYS \
  -e PRICING_ENGINE_CONFIG_PATH=/app/configs/base.yaml \
  ai-bank-pricing-engine
```

The image:

- installs the package with the `api` extra only (no dev/ml/llm dependencies);
- runs as a non-root user (`pricing`);
- exposes port `8000` and declares a container `HEALTHCHECK` against `/health`;
- explicitly sets `PRICING_ENGINE_API_HOST=0.0.0.0` inside the container and refuses
  startup without valid `PRICING_ENGINE_API_KEYS`; the host port above remains local;
- excludes `.env` and `.env.*` from its build context and never bakes keys into the image;
- fails to start serving meaningful traffic if the configured YAML is invalid — `/ready`
  will report `503` immediately, which orchestrators should wire into their own readiness
  gate rather than restarting the container in a crash loop.

To persist run ledgers outside the container, mount a volume and point
`persist_ledger` requests at it via `ledger_base_dir` (currently only configurable through
the application-layer request, not an environment variable — see *Persistence* below):

```bash
export PRICING_ENGINE_API_KEYS="$(openssl rand -hex 32)"
docker run --rm -p 127.0.0.1:8000:8000 \
  -e PRICING_ENGINE_API_KEYS \
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

### API request limits

Both POST routes reject excessive work with HTTP **422 before simulation or optimisation**.
These limits apply only to API requests; trusted local CLI configuration remains compatible.
The operator may override the positive integer environment variables below. The supported
launcher validates them at startup; malformed runtime settings fail closed with HTTP 500.
Client YAML cannot override these limits or the fixed two adaptive expansion rounds.

| Environment variable | Default | Bound |
| --- | ---: | --- |
| `PRICING_ENGINE_API_MAX_REQUEST_BYTES` | 524288 | Actual JSON body bytes, including streamed/chunked bodies, before JSON parsing |
| `PRICING_ENGINE_API_MAX_YAML_BYTES` | 65536 | UTF-8 encoded YAML bytes |
| `PRICING_ENGINE_API_MAX_YAML_DEPTH` | 24 | Collection nesting; operator maximum 64 |
| `PRICING_ENGINE_API_MAX_YAML_NODES` | 4096 | YAML parser events before construction |
| `PRICING_ENGINE_API_MAX_LOAN_CUSTOMERS` | 25000 | Synthetic loan customers |
| `PRICING_ENGINE_API_MAX_DEPOSIT_CUSTOMERS` | 25000 | Synthetic deposit customers |
| `PRICING_ENGINE_API_MAX_GRID_POINTS` | 501 | Points per dimension, including worst-case expansion |
| `PRICING_ENGINE_API_MAX_GRID_SIZE` | 25000 | Combined grid pairs per round, including expansion |
| `PRICING_ENGINE_API_MAX_ADAPTIVE_EVALUATIONS` | 40000 | Cumulative pairs over initial grid plus both expansion rounds |
| `PRICING_ENGINE_API_MAX_SENSITIVITY_EVALUATIONS` | 500000 | Cumulative pairs across main run and all default sensitivity cases |
| `PRICING_ENGINE_API_LIMIT_CONCURRENCY` | 4 | Uvicorn concurrent connection/task limit; maximum 128 |

For example, lower the customer and body limits for a small local host:

```bash
export PRICING_ENGINE_API_MAX_LOAN_CUSTOMERS=10000
export PRICING_ENGINE_API_MAX_DEPOSIT_CUSTOMERS=10000
export PRICING_ENGINE_API_MAX_YAML_BYTES=32768
python3 -m balance_pricing_engine.api.server
```

Work estimates are deliberately conservative: they allow for six-decimal grid rounding,
both possible boundary directions, and repeated evaluation in expansion. Segmented grids
use the product of summed tier dimensions as an upper bound. Sensitivity currently has
28 default cases plus the endpoint's main run. The canonical 5,000/5,000-customer scenario,
including sensitivity, fits the defaults. A request near a budget may be refused even if
its realised optimum would have stopped expansion early. Unsupported or malformed grids
also return 422. YAML aliases, duplicate/non-string mapping keys and recursive structures
are refused.

Concurrency is enforced **at launch time by uvicorn**, not by an application semaphore.
Over-cap requests receive uvicorn's HTTP 503. Body and computation limits remain enforced
in the application. This is not per-client throttling, a CPU deadline or a global disk quota;
review ingress limits, request timeouts and ledger retention before any network deployment.

### Filesystem access and response compatibility

API YAML cannot override `competitor_rates.source_file` or
`pricing_intelligence.database_path`. The exact canonical default strings remain accepted
for compatibility with `configs/base.yaml`; arbitrary absolute or relative overrides are
rejected even when the feature is disabled. No rejected path is resolved or probed. Trusted
local CLI workflows may continue using custom paths.

Validation and execution errors do not echo raw YAML, exception text, submitted values or
server paths. The versioned error schema and legacy `detail` shapes remain intact, with
sanitised messages. `/ready` retains 200/503 semantics but reports only the config basename.
Versioned `run_dir` remains a string, now a relative `data/outputs/runs/<run-name>` reference,
never an absolute server path. This is an intentional compatibility restriction.

Submitted and resolved competitor-overlay configs use exclusive mode-0600 temporary files.
Every request owns its file, cleans it up on success or failure, and preserves the existing
snapshot/hash provenance. This does not change the ledger's existing file permissions or
retention policy. Pricing mathematics, model approval controls and governance gates are unchanged.

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

## 6. Local dashboard and Operations

Launch from the repository root:

```bash
python3 -m pip install -e ".[dashboard]"
python3 -m streamlit run src/balance_pricing_engine/dashboard/app.py --server.address 127.0.0.1
```

The project `.streamlit/config.toml` pins `server.address="127.0.0.1"`, and the private
`Pricing Watch.command` launcher also pins loopback. Streamlit CLI flags override environment
and TOML configuration. Operations checks the **effective** address when rendering and again
in every administrative callback. Without a token, only explicit numeric loopback bindings
permit actions; unknown, malformed, hostname and non-loopback bindings fail closed.

Require a shared Operations token locally:

```bash
export PRICING_WATCH_OPS_TOKEN="$(openssl rand -hex 32)"
python3 -m streamlit run src/balance_pricing_engine/dashboard/app.py --server.address 127.0.0.1
```

Enter that token in Operations & Trust to unlock maintenance and collection controls.
Tokens shorter than 16 characters disable Operations. The attempt limiter, action cooldowns,
single-flight runner and explicit live-collection confirmation remain in force.

The token authenticates **Operations only**, not read-only market pages or the whole dashboard.
If deployed beyond loopback, require both this token and independently reviewed authenticated
ingress/TLS covering every page, plus network controls. A loopback binding behind a publicly
reachable proxy or SSH tunnel is still reachable through that intermediary; bind address alone
does not authenticate those users. Do not expose the dashboard by overriding its address alone.
See [Pricing Watch dashboard](pricing_intelligence.md#dashboard) for the full operating guide.

## 7. Production boundaries (not implemented)

The following are explicitly out of scope for this prototype and must be addressed before
any real production use:

- **Authentication/authorisation.** The supported API launcher requires rotating shared
  API keys for non-loopback bindings. Put remote access behind independently reviewed
  authenticated ingress, TLS and network controls. Shared keys provide no user roles or
  per-client quotas. `GET /health` and `/ready` stay open for probes; `/docs` remains open.
  Loopback-only API use may omit keys. The dashboard's Operations token protects actions only.
- **Secrets management.** Two secrets are actively read today through
  `secrets.provider.SecretsProvider` — protocol: `get_secret(name) -> str | None`. Both
  default to `EnvSecretsProvider`, reading environment variables (still not committed to
  source control):
  - `ANTHROPIC_API_KEY` (used by the optional `[llm]` layer) — injected per call via
    `llm.build_client(secrets_provider=...)`/`llm.is_llm_available(secrets_provider=...)`
    (both exported from `balance_pricing_engine.llm`), so a future Vault/AWS Secrets
    Manager/GCP Secret Manager adapter implementing the same protocol can be passed in
    without changing call sites.
  - `PRICING_ENGINE_API_KEYS` (the API-key auth described above) — resolved through
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
  export PRICING_ENGINE_API_KEYS="$(openssl rand -hex 32)"
  docker run --rm -p 127.0.0.1:8443:8000 \
    -e PRICING_ENGINE_API_KEYS \
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

All output remains **synthetic-data decision support**. These engineering controls grant no
real banking pricing approval, model-risk approval or regulatory approval. No automatic pricing
action is implemented or authorised. Production use requires independent model validation,
calibration and network-security review beyond this change.
