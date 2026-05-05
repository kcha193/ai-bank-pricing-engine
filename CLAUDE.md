# AI Bank Pricing Engine — Claude Instructions

> Companion to [AGENTS.md](AGENTS.md). Source of truth for commands and scope is AGENTS.md; this file adds Claude-specific architectural detail verified against the code.

## What this project is

Python retail bank balance-sheet pricing engine. **Synthetic-data only.** **Portfolio-level** (one home-loan rate, one term-deposit rate) — not personalised. NZ context (default competitor banks: ANZ, ASB, Westpac, BNZ, Kiwibank).

The engine maximises expected risk-adjusted profit subject to a funding-shortfall penalty, via exhaustive joint grid search with adaptive boundary expansion. A separate diagnostic ML layer trains response models on simulated outcomes and checks they recover sensible rate-response behaviour.

## Module map (verified against [src/balance_pricing_engine/](src/balance_pricing_engine/))

- [config.py](src/balance_pricing_engine/config.py) — Pydantic v2 `EngineConfig` + `ResponseCoefficientsConfig`. Validates that **exactly one** of `pricing_strategy` (market-relative spreads) or `products` (absolute grids) is provided. Refuses spread grids that would generate negative rates. `model_validator(mode="after")` emits `warnings.warn` for: negative `wholesale_funding_spread`, penalty dominated by funding spread, `n_*_customers < 1000`, spread band < 50 bps.
- [market.py](src/balance_pricing_engine/market.py) — `MarketRates`, `SpreadGridConfig`, `bps_to_rate`, `build_market_relative_grid`.
- [rate_grids.py](src/balance_pricing_engine/rate_grids.py) — `build_configured_rate_grids`: dispatches between market-relative spread grids and absolute fallback grids.
- [synthetic/](src/balance_pricing_engine/synthetic/) — `customers.py` (loan + deposit generators), `market.py` (`MarketState`), `response.py` (`loan_acceptance_probability`, `deposit_uptake_probability`).
- [pricing/](src/balance_pricing_engine/pricing/) — `profit.py` (loan income, ECL, deposit cost, wholesale funding) + `balance_sheet.py` (`calculate_funding_result`, shortfall + penalty).
- [optimization/grid_search.py](src/balance_pricing_engine/optimization/grid_search.py) — `evaluate_joint_rate_grid`, `select_best_result`, `optimize_joint_rates`, `optimize_joint_market_relative_rates`. Returns `OptimizationResult` dataclass with backward-compat aliases (`objective_value`, `deposit_cost`, `funding_penalty`).
- [diagnostics.py](src/balance_pricing_engine/diagnostics.py) — `BoundaryDiagnostics`, `ObjectiveDecomposition`, `evaluate_adaptive_joint_rate_grid` (boundary-expansion wrapper), `run_sensitivity_analysis` (returns `delta_loan_rate_bps`, `delta_deposit_rate_bps`, `delta_objective_pct`, `parameter_family`; accepts `n_seeds` for Monte Carlo cohort uncertainty). Default sensitivity cases perturb all four elasticities and two customer-feature coefficients by ±25%.
- [scenarios/runner.py](src/balance_pricing_engine/scenarios/runner.py) — `run_baseline_diagnostics`, `ScenarioDiagnostics` dataclass, `write_artifacts()` persistence. `ScenarioDiagnostics` also exposes `counterfactual_decompositions(top_n)` and `cohort_attribution()`.
- [scenarios/experiments.py](src/balance_pricing_engine/scenarios/experiments.py) — small scenario helpers (e.g. `run_higher_funding_penalty_scenario`).
- [modelling/](src/balance_pricing_engine/modelling/) — diagnostic ML layer:
  - `datasets.py` — feature schemas (`LOAN_FEATURE_COLUMNS`, `DEPOSIT_FEATURE_COLUMNS`) and dataset builders.
  - `models.py` — `build_logistic_regression_model`, `build_hist_gradient_boosting_model`, `fit_response_model`, `predict_response_probability`.
  - `evaluation.py` — `evaluate_probabilities(y_true, y_probability)` (pure metrics dict), `evaluate_model(model, frame, features, target)` (thin wrapper), `evaluate_calibration`.
  - `persistence.py` — joblib `save_model` / `load_model`.
  - `agentic.py` — **governed agentic workflow**: `ModelTrainingAgent`, `ModelScoringAgent`, `ModelRegistry`, `ModelRecord`, `TrainingConfig`, `TrainingResult`, plus `ElasticityBaselineModel`, `ElasticityResponseModel`, `LogisticResponseModel`. Single `train(*, dataset, candidates, registry, ...)` entry point replacing the former dual `train` / `train_and_recommend` API.
  - `workflows.py`, `reports.py` — orchestration + reporting helpers.
- [competitor_rates/](src/balance_pricing_engine/competitor_rates/) — ingestion pipeline with its own CLI ([cli.py](src/balance_pricing_engine/competitor_rates/cli.py)) and subcommands `fetch | validate | apply | report`. Backed by `sources.py` (official bank pages), `extractors.py`, `normalise.py`, `validate.py`, `store.py`, `apply.py`.
- [reporting.py](src/balance_pricing_engine/reporting.py), [plotting.py](src/balance_pricing_engine/plotting.py) — output formatting.
- [utils/random.py](src/balance_pricing_engine/utils/random.py) — `make_rng` for reproducible RNGs.

Full flow diagram: [docs/architecture.md](docs/architecture.md). Modelling assumptions: [docs/modelling_assumptions.md](docs/modelling_assumptions.md). Governance: [docs/model_governance.md](docs/model_governance.md). Response model card: [docs/response_model_card.md](docs/response_model_card.md). Competitor ingestion: [docs/competitor_rate_ingestion.md](docs/competitor_rate_ingestion.md).

## Response model — dual elasticity

Both loan acceptance and deposit uptake use **two elasticities** (see [configs/base.yaml](configs/base.yaml) `response:` block):

- `loan_rate_elasticity` — own-rate sensitivity (negative for loans).
- `loan_competitor_gap_elasticity` — sensitivity to gap vs competitor loan rate.
- `deposit_rate_elasticity` — own-rate sensitivity (positive for deposits).
- `deposit_competitor_gap_elasticity` — sensitivity to gap vs competitor deposit rate.

Refactors that collapse these into a single elasticity term are **not equivalent** and will silently change recommendations.

## Commands

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
python3 -m ruff check .
python3 examples/run_baseline.py
python3 examples/run_with_competitor_rates.py
python3 examples/train_response_models.py

# Competitor-rate CLI (separate subcommand surface):
python3 -m balance_pricing_engine.competitor_rates.cli fetch    --output-dir data/processed/competitor_rates
python3 -m balance_pricing_engine.competitor_rates.cli validate --run-file <path>
python3 -m balance_pricing_engine.competitor_rates.cli apply    --config configs/base.yaml
python3 -m balance_pricing_engine.competitor_rates.cli report   --run-file <path>
```

`live_rates` pytest marker is opt-in — fetches **real** bank pages. Do not run in CI or without intent.

## Configuration

[configs/base.yaml](configs/base.yaml) is the canonical config. Key shapes:

- `random_seed: 42`, `simulation.n_loan_customers: 5000`, `simulation.n_deposit_customers: 5000`.
- `market.*` includes `cash_rate`, `wholesale_funding_spread`, competitor rates, and reference rates required when using `pricing_strategy`.
- `response.coefficients.loan.*` and `response.coefficients.deposit.*` — intercepts, rate anchors, and feature coefficients wired into `synthetic/response.py` (see `ResponseCoefficientsConfig`).
- Choose **one**: `pricing_strategy.{home_loan,term_deposit}_spread_bps` (market-relative, default) **or** `products.{loan,deposit}_rate_grid` (absolute).
- `competitor_rates.enabled: false` by default — when true, reads from `data/processed/competitor_rates/latest.json` with staleness + strict-mode checks.
- `balance_sheet.required_deposit_funding_ratio` and `funding_shortfall_penalty_rate` drive the funding penalty.

## Conventions (observed)

- Python ≥3.9, ruff `line-length=100`, `target-version="py39"`.
- Pydantic **v2**. NumPy pinned `<2.0`. pandas ≥2.1. scikit-learn ≥1.4. joblib ≥1.4.
- `pythonpath = ["src"]` (set in [pyproject.toml](pyproject.toml)) — import as `balance_pricing_engine.*`, **not** `src.balance_pricing_engine.*`.
- Tests live under [tests/unit/](tests/unit/) (one module per source file). Fixtures under [tests/fixtures/](tests/fixtures/).
- Workflow scripts (long-running) → [scripts/](scripts/). Short demos → [examples/](examples/).
- Data folders: [data/raw/](data/raw/) (untouched inputs), [data/processed/](data/processed/) (cleaned), [data/outputs/](data/outputs/) (run artifacts).
- Notebook walkthrough: [notebooks/bank_pricing_engine_walkthrough.ipynb](notebooks/bank_pricing_engine_walkthrough.ipynb).

## Regression contract — do not break

`ScenarioDiagnostics.write_artifacts()` produces a stable artifact set consumed by regression checks:

- `grid_results.csv` — every final evaluated rate pair (columns enumerated in `GRID_RESULT_COLUMNS`).
- `top_results.csv` — top-N by `total_objective`.
- `sensitivity.csv` — when sensitivity provided (includes `delta_loan_rate_bps`, `delta_deposit_rate_bps`, `delta_objective_pct`, `parameter_family`).
- `counterfactuals.csv` — when `counterfactual_decompositions()` is called.
- `cohort_attribution.csv` — when `cohort_attribution()` is called.
- `summary.json` — keys include `best_loan_rate`, `best_deposit_rate`, `objective_value`, `expected_loan_volume`, `expected_deposit_volume`, `funding_shortfall`, `excess_deposit_income`, **`boundary_warning_flag`**, **`initial_boundary_warning_flag`**, **`expansion_rounds`**, `expansion_capped`, `policy_band_breached`.

The boundary fields distinguish "needed expansion" from "still hitting boundary after expansion" — preserve their semantics when refactoring. `excess_deposit_income` credits surplus deposits at `cash_rate` when deposit volume exceeds loan volume.

## When making changes

- Honour the synthetic-data-only invariant. Do not add real customer or counterparty data paths.
- Keep portfolio-level scope unless the user explicitly opens up personalised pricing.
- `OptimizationResult` is a frozen dataclass; backward-compat aliases (`objective_value`, `deposit_cost`, `funding_penalty`, `loan_income_after_losses`) exist deliberately — don't remove without checking call sites.
- Modelling layer is **diagnostic only** — its predictions are not yet wired into the optimiser. Don't conflate ML response models with the synthetic response functions used inside the grid search. The single `train()` entry point in `agentic.py` replaced the former dual `train` / `train_and_recommend` API; `evaluate_binary_classifier` is gone — use `evaluate_probabilities` / `evaluate_model` from `evaluation.py`.
- Competitor-rate ingestion is gated behind `competitor_rates.enabled` and `strict` flags. Strict-mode failures should error, not warn.
- Update [docs/architecture.md](docs/architecture.md) and [docs/modelling_assumptions.md](docs/modelling_assumptions.md) when changing structural behaviour.

## Roadmap & recent reviews

- Roadmap: [docs/agentic_modelling_roadmap.md](docs/agentic_modelling_roadmap.md).
- Latest diagnostic review: [docs/diagnostic_review_2026-04-26.md](docs/diagnostic_review_2026-04-26.md).
- Step-by-step business+technical walkthrough: [docs/usage_guide.md](docs/usage_guide.md) (includes restricted-network setup).
