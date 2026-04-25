# AI Balance Sheet Pricing Engine

A production-style Python project for simulating and optimizing retail bank pricing across:

- Home lending rates, as asset pricing
- Term deposit rates, as liability pricing

The first milestone is intentionally small: synthetic customer generation, known elasticity response
simulation, risk-adjusted profit calculation, a funding constraint, and a transparent joint grid
search optimizer.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
python examples/run_baseline.py
```

## Current Scope

The current engine chooses one portfolio-level home loan rate and one portfolio-level term deposit
rate. It maximizes expected risk-adjusted profit while penalizing funding shortfalls.

Machine learning, FastAPI, and dashboards come later. The foundation starts with correct economics
and testable synthetic behaviour.

