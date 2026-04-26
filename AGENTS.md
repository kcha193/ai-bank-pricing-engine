# AI Bank Pricing Engine — Codex Instructions

## Project context

This is a Python retail bank balance-sheet pricing engine.

The current scope is portfolio-level pricing, not personalised pricing.

The engine simulates and optimises:
- Home loan rate
- Term deposit rate

The project uses synthetic data only.

## Important commands

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
python3 -m ruff check .
python3 examples/run_baseline.py
python3 examples/train_response_models.py