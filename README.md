# AI Balance Sheet Pricing Engine

A production-style Python project for simulating and optimizing retail bank pricing across:

- Home lending rates, as asset pricing
- Term deposit rates, as liability pricing

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
python3 examples/run_baseline.py
python3 examples/train_response_models.py
```

If your environment cannot download build dependencies, see the restricted-network setup notes in
[docs/usage_guide.md](docs/usage_guide.md#restricted-network-setup).

For a step-by-step walkthrough written for both business and technical users, see
[docs/usage_guide.md](docs/usage_guide.md).

For model-risk and architecture notes, see:

- [docs/model_governance.md](docs/model_governance.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/competitor_rate_ingestion.md](docs/competitor_rate_ingestion.md)

## Current Scope

The engine chooses one portfolio-level home loan rate and one portfolio-level term deposit rate,
maximising expected risk-adjusted profit while penalising funding shortfalls.

The project also includes:

- A diagnostic ML layer that trains response models on synthetic observed outcomes and validates
  rate-response behaviour.
- An LLM decision-support layer (`llm/`) for plain-language narration and config anomaly detection
  (human review required — never wired into automated decisions).
- A FastAPI endpoint (`POST /run-scenario`) in `api/` for local HTTP access.

Every pricing run produces an auditable ledger under `data/outputs/runs/` containing a config
snapshot, run metadata, a business-facing pricing memo, and a governance checklist — all generated
offline with no external dependencies. See [docs/run_ledger_guide.md](docs/run_ledger_guide.md).

Dashboards and production deployment are not yet implemented.
