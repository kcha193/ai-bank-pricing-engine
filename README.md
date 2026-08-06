# AI Balance Sheet Pricing Engine

A production-style Python project for simulating and optimizing retail bank pricing across:

- Home lending rates, as asset pricing
- Term deposit rates, as liability pricing

## Quickstart

Requires Python ≥ 3.9.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
python3 examples/run_baseline.py               # core grid-search + diagnostics
python3 examples/run_governed_scenario.py      # governed PricingService path (decision + gates)
python3 -m balance_pricing_engine.competitor_rates.cli fetch --output-dir data/processed/competitor_rates
python3 examples/run_with_competitor_rates.py  # with live competitor rate ingestion
python3 examples/run_llm_diagnostics.py        # LLM scenario narration (requires ANTHROPIC_API_KEY)
python3 examples/run_multiperiod.py            # multi-period scenario
python3 examples/train_response_models.py      # diagnostic ML layer
```

If your environment cannot download build dependencies, see the restricted-network setup notes in
[docs/usage_guide.md](docs/usage_guide.md#restricted-network-setup).

For a step-by-step walkthrough written for both business and technical users, see
[docs/usage_guide.md](docs/usage_guide.md).

For model-risk and architecture notes, see:

- [docs/model_governance.md](docs/model_governance.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/competitor_rate_ingestion.md](docs/competitor_rate_ingestion.md)
- [docs/deployment.md](docs/deployment.md) — local and Docker deployment, health/readiness,
  production boundaries
- [docs/adr/](docs/adr/) — architecture decision records for the modular-monolith design

## Current Scope

The engine chooses one portfolio-level home loan rate and one portfolio-level term deposit rate,
maximising expected risk-adjusted profit while penalising funding shortfalls.

The project is structured as a modular monolith (see
[docs/adr/0001-modular-monolith-before-microservices.md](docs/adr/0001-modular-monolith-before-microservices.md)):
one Python package with clear internal boundaries. Callers should prefer
`application.PricingService` over calling `scenarios.run_baseline_diagnostics()` directly, since
it adds governance evaluation, model/feature lineage, decision records, and event publication
around the same domain math. See [docs/architecture.md](docs/architecture.md) for the full flow.

The project also includes:

- A diagnostic ML layer that trains response models on synthetic observed outcomes and validates
  rate-response behaviour, governed by a local `ModelRegistry` approval/champion workflow.
- A lightweight, versioned feature-store boundary (`features/`) so training and serving features
  are demonstrably drawn from the same schema, with a fingerprint recorded in lineage.
- A governance control plane (`governance/`) that evaluates typed gates (boundary warnings, policy
  band, funding shortfall, model/feature compatibility, etc.), derives a `RECOMMENDED` /
  `REVIEW_REQUIRED` / `BLOCKED` decision status, and writes an additive `decision_record.json`.
- An in-process domain-event seam (`events/`) with local implementations
  (no-op / in-memory / JSONL) — a future Kafka/Pub/Sub adapter can implement the same protocol.
- An LLM decision-support layer (`llm/`) for plain-language narration and config anomaly detection
  (human review required — never wired into automated decisions).
- A FastAPI serving layer (`api/`) with the original `POST /run-scenario` endpoint (stable,
  backward-compatible) and a versioned `POST /v1/pricing/scenarios` endpoint backed by
  `PricingService`, plus `GET /health` and `GET /ready`.

Every pricing run produces an auditable ledger under `data/outputs/runs/` containing a config
snapshot, run metadata, a business-facing pricing memo, a governance checklist, and (when run
through `PricingService`) an additive decision record — all generated offline with no external
dependencies. See [docs/run_ledger_guide.md](docs/run_ledger_guide.md).

A minimal, secure local Docker deployment path exists (see
[docs/deployment.md](docs/deployment.md)). Dashboards remain a `streamlit` extra
(`dashboard/`); full production deployment (authentication, TLS, durable event/feature/model
stores, autoscaling, enterprise monitoring) is out of scope — see the production-boundaries list
in [docs/deployment.md](docs/deployment.md).
