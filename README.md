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

# NZ public-market pricing intelligence (market observation only — never prices anything)
python3 -m balance_pricing_engine.pricing_intelligence collect --render-javascript
python3 -m balance_pricing_engine.pricing_intelligence snapshot
python3 -m balance_pricing_engine.pricing_intelligence ocr           # RBNZ Official Cash Rate
python3 -m balance_pricing_engine.pricing_intelligence run --dry-run # full operational cycle, no persistence/alerts
python3 -m balance_pricing_engine.pricing_intelligence brief         # daily intelligence brief
python3 -m balance_pricing_engine.pricing_intelligence health        # DB/collector/alert-channel status
python3 examples/run_market_intelligence_scenarios.py
python3 examples/run_llm_diagnostics.py        # LLM scenario narration (requires ANTHROPIC_API_KEY)
python3 examples/run_multiperiod.py            # multi-period scenario
python3 examples/train_response_models.py      # diagnostic ML layer
```

Optional extras (`pip install -e ".[extra]"`, comma-separate for more than one):

| Extra | Adds |
| --- | --- |
| `dev` | test/lint tooling plus everything needed to run the suite locally |
| `api` | FastAPI + uvicorn for the serving layer |
| `dashboard` | Streamlit dashboard (`scripts/streamlit_dashboard.py`) |
| `llm` | Anthropic client for the decision-support narration layer |
| `ml` | LightGBM/XGBoost/lifelines candidate models for the diagnostic ML layer |
| `kafka` | `KafkaEventPublisher` for the domain-event seam |
| `browser` | Playwright, required for live competitor-rate/market-intelligence collection (`--render-javascript`) |

Every optional environment variable (alert webhooks, Telegram, `ANTHROPIC_API_KEY`, API auth keys,
pricing-intelligence DB path) is documented in [.env.example](.env.example) — each gated feature is
off by default and the engine runs with none of it set.

If your environment cannot download build dependencies, see the restricted-network setup notes in
[docs/usage_guide.md](docs/usage_guide.md#restricted-network-setup).

For a step-by-step walkthrough written for both business and technical users, see
[docs/usage_guide.md](docs/usage_guide.md).

For model-risk and architecture notes, see:

- [docs/model_governance.md](docs/model_governance.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/competitor_rate_ingestion.md](docs/competitor_rate_ingestion.md)
- [docs/pricing_intelligence.md](docs/pricing_intelligence.md)
- [docs/deployment.md](docs/deployment.md) — local and Docker deployment, health/readiness,
  production boundaries
- [docs/adr/](docs/adr/) — architecture decision records for the modular-monolith design
- [CHANGELOG.md](CHANGELOG.md) — release history

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
- An in-process domain-event seam (`events/`) with no-op / in-memory / JSONL implementations plus
  a `KafkaEventPublisher` (optional `kafka` extra) — a broken or slow publisher is logged and
  never fails, blocks, or alters a pricing decision.
- An LLM decision-support layer (`llm/`) for plain-language narration and config anomaly detection
  (human review required — never wired into automated decisions).
- A FastAPI serving layer (`api/`) with the original `POST /run-scenario` endpoint (stable,
  backward-compatible) and a versioned `POST /v1/pricing/scenarios` endpoint backed by
  `PricingService`, plus `GET /health` and `GET /ready`. Both `POST` routes accept an optional
  `X-API-Key` header, enforced only when `PRICING_ENGINE_API_KEYS` is set.
- A **pricing-intelligence operations layer** (`pricing_intelligence/`) that turns NZ public-market
  observation into a daily operating workflow, entirely separate from pricing itself: scheduled
  collection (`scripts/collect_market_rates_workflow.py`, launchd/cron examples under
  `deploy/scheduling/`), a persisted operational run ledger, per-bank collector health monitoring
  with escalation, webhook and Telegram alert delivery, daily/weekly intelligence briefs, decision-
  support scenarios auto-triggered on a meaningful rate move, and database backup/health-check
  commands. See [docs/pricing_intelligence.md](docs/pricing_intelligence.md)'s "Pricing
  Intelligence Operations" section. It also tracks the RBNZ Official Cash Rate (`ocr` command)
  alongside the bank-rate collectors. This layer only ever answers "what is the market doing?" —
  it cannot import the optimiser and never changes a price on its own.

Every pricing run produces an auditable ledger under `data/outputs/runs/` containing a config
snapshot, run metadata, a business-facing pricing memo, a governance checklist, and (when run
through `PricingService`) an additive decision record — all generated offline with no external
dependencies. See [docs/run_ledger_guide.md](docs/run_ledger_guide.md).

A minimal, secure local Docker deployment path exists (see
[docs/deployment.md](docs/deployment.md)). The Streamlit dashboard remains an optional `dashboard`
extra; full production deployment (authentication, TLS, durable event/feature/model stores,
autoscaling, enterprise monitoring) is out of scope — see the production-boundaries list in
[docs/deployment.md](docs/deployment.md).
