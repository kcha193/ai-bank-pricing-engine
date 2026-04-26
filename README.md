# AI Balance Sheet Pricing Engine

A production-style Python project for simulating and optimizing retail bank pricing across:

- Home lending rates, as asset pricing
- Term deposit rates, as liability pricing

The first milestone is intentionally small: synthetic customer generation, known elasticity response
simulation, risk-adjusted profit calculation, a funding constraint, and a transparent joint grid
search optimizer.

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

## Current Scope

The current engine chooses one portfolio-level home loan rate and one portfolio-level term deposit
rate. It maximizes expected risk-adjusted profit while penalizing funding shortfalls.

The project also includes a small diagnostic ML layer that trains response models on synthetic
observed outcomes and checks whether learned predictions recover sensible rate-response behaviour.
FastAPI, dashboards, and production model integration come later.
