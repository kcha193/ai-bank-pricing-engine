# Diagnostic Review — 2026-04-26

_Updated 2026-05-13 to reflect items addressed in [PR #20](https://github.com/kcha193/ai-bank-pricing-engine/pull/20)._

## 1) Repository structure and current engine summary

- `src/balance_pricing_engine/synthetic/` creates synthetic customers and known response curves.
- `src/balance_pricing_engine/pricing/` computes economic components: loan income, credit loss, deposit cost, wholesale funding cost, and funding shortfall penalty.
- `src/balance_pricing_engine/optimization/grid_search.py` performs exhaustive 2D grid evaluation across home-loan and term-deposit rates, then selects the highest objective.
- `src/balance_pricing_engine/scenarios/runner.py` orchestrates baseline scenario runs and diagnostics.
- `src/balance_pricing_engine/diagnostics.py` provides objective decomposition, boundary warnings, and one-at-a-time sensitivity analysis.
- `src/balance_pricing_engine/modelling/` builds synthetic observed datasets and trains diagnostic-only response models.
- `examples/run_baseline.py` and `examples/train_response_models.py` are executable walkthroughs.

Current engine behaviour:

- Single portfolio-level home-loan rate and single portfolio-level term-deposit rate are optimized jointly (no segment/personalized pricing yet).
- Objective:
  - `+` loan interest income
  - `-` expected credit loss
  - `-` deposit interest expense
  - `-` wholesale funding cost for loan volume not funded by deposits
  - `-` penalty for shortfall against required deposit-funding ratio.
  - `+` excess deposit income when deposits exceed lending (surplus earns cash rate).
- Pricing grids are market-relative spread grids by default (basis points around reference rates).

## 2) Setup and run diagnostics requested

### Command outcomes

1. `python3 -m pip install -e ".[dev]"`
   - **Failed in this environment**: pip could not download build dependency `setuptools>=64` due proxy/network restrictions. See [docs/usage_guide.md](usage_guide.md) for offline wheelhouse instructions.
2. `python3 -m pytest -q`
   - **Passed**: 33 passed (201 passed as of 2026-05-13).
3. `python3 -m ruff check .`
   - **Passed**: all checks passed.
4. `python3 examples/run_baseline.py`
   - **Passed** and produced baseline optimization output.
5. `python3 examples/train_response_models.py`
   - **Passed** and produced model metrics/calibration/sanity checks.

### Does the project run successfully?

- **Yes**: tests, lint, baseline scenario, and model-training example all run successfully.
- Offline/restricted-network setup is now documented in [docs/usage_guide.md](usage_guide.md).

## 3) Modelling review from retail bank balance-sheet pricing perspective

### Home loan pricing

- Acceptance probability declines with offered loan rate and worsens versus competitor rate gap.
- Utility also depends on risk score, credit score, and LVR.
- Commercially plausible directional behaviour is implemented.
- Concern: elasticity and intercept are hard-coded constants; no explicit governance/calibration process to empirical portfolio data.

### Term deposit pricing

- Uptake probability increases with offered deposit rate and versus competitor gap.
- Includes relationship tenure, digital engagement, and rate sensitivity features.
- Directional behaviour is sensible; however, deposit utility is still a static single-period formulation.

### Customer response / elasticity assumptions

- Transparent and testable; good for prototype.
- Material model-risk concern: same synthetic DGP is used for both optimization and training-data generation (self-consistency bias).
- No explicit uncertainty intervals around elasticity parameters.

### Expected credit loss (ECL)

- ECL represented as expected annual loss rate (`expected_loss_rate`) times accepted balance.
- Useful first-order approximation, but no staging, macro-scenario overlays, downturn LGD, cure/recovery timing, or maturity profile.

### Wholesale funding cost

- Applied to `max(loan_volume - deposit_volume, 0)` at cash + spread.
- Economically coherent for marginal unsecured funding approximation.
- Missing term-structure, liquidity premiums, transfer-pricing curve, and basis dynamics.

### Deposit funding shortfall

- Separate policy-style penalty on shortfall versus required deposit funding ratio.
- Intentionally additive with wholesale funding cost: wholesale cost captures marginal funding economics; penalty enforces the strategic deposit-funding ratio target. Documented in [docs/usage_guide.md](usage_guide.md).

### Objective function

- Clear and decomposable; `architecture.md` diagram updated to include excess deposit income term.
- In baseline output objective is materially negative with very large shortfall, suggesting configuration mismatch between demand, grids, and funding constraints rather than optimizer failure.
- No capital/RWA constraints, NII volatility, customer fairness, or volume growth targets.

### Optimizer grid design

- Exhaustive grid search is transparent and robust for small grids.
- Baseline winner lands on max boundary for both products: strong sign grid is too narrow and/or objective/elasticities produce monotonic push to boundaries.
- Adaptive boundary expansion (`evaluate_adaptive_joint_rate_grid`) auto-expands the grid when the winner hits an edge.
- Tie-break policy: lowest funding shortfall first, then lowest combined absolute spread to market. Documented in [docs/model_governance.md](model_governance.md).

### Boundary diagnostics

- Diagnostics correctly warn when winner is at min/max boundary.
- Adaptive re-grid is implemented; `summary.json` distinguishes `initial_boundary_warning_flag` from `boundary_warning_flag` post-expansion.

### Sensitivity analysis

- Includes one-at-a-time perturbations to elasticities, funding penalty/rate, and competitor rates.
- Two-way stress scenarios added: elasticity × funding penalty, competitor loan rate × wholesale rate, competitor deposit rate × funding penalty.
- In current baseline all scenarios still hit upper boundaries, which weakens decision confidence (insufficiently informative search range).

## 4) Identified issues

### Bugs / defects

1. ✅ **API-level grid bug** — `build_rate_grid` now validates step, bounds, and divisibility via `grid_utils.build_inclusive_numeric_grid`. Raises `ValueError` on misuse.
2. ✅ **Code duplication** — `grid_search.py`, `market.py`, and `diagnostics.py` all delegate to `grid_utils.build_inclusive_numeric_grid`; inline `n_steps`/`np.arange` pattern removed.
3. ⬜ **Example import hack** — examples still modify `sys.path` manually. Low risk given `pythonpath = ["src"]` in `pyproject.toml`; defer to a packaging cleanup pass.

### Missing tests

- ✅ Unit tests for invalid `build_rate_grid` inputs (zero/negative step, reversed bounds, non-divisible step) — added in `test_optimizer`, `test_market`, `test_grid_utils`.
- ✅ Objective interaction tests (wholesale cost + funding penalty combined effect) — `test_objective_properties.py`.
- ✅ Tie-breaking behaviour — `test_optimizer.py::test_select_best_result_prefers_lower_funding_shortfall_when_objectives_tie` and sibling test.
- ✅ Two-way sensitivity stresses — `test_diagnostics.py::test_default_sensitivity_includes_two_way_stresses`.
- ⬜ Explicit boundary-warning remediation workflow tests — adaptive re-grid exists but no dedicated test for the expand-then-recheck path.

### Unclear assumptions

- ✅ Wholesale funding cost vs shortfall penalty interaction — now documented in `usage_guide.md` as intentionally additive with distinct economic roles.
- ⬜ Interpretation of required deposit funding ratio as hard strategic target vs soft risk appetite metric — still implicit in config; no governance note yet.
- ⬜ Annualized single-period treatment for both loans and deposits — no tenor dynamics; acknowledged in `modelling_assumptions.md`.

### Model-risk concerns

- Synthetic-data circularity: optimization and ML diagnostics rely on same stylized latent model.
- Elasticities are point estimates without confidence bounds/priors.
- No challenger models for response, credit loss, or funding.
- No fairness/conduct constraints in pricing decisions.

### Documentation gaps

- ✅ Offline/air-gapped installation — wheelhouse instructions fixed in `usage_guide.md`.
- ✅ Architecture diagram updated with excess deposit income term.
- ✅ Tie-break policy documented in `model_governance.md`.
- ⬜ Model-governance note for parameter calibration cadence and approval process — partially in `model_governance.md`; empirical calibration process not yet defined.

## 5) Ranked action plan

### Must fix now — completed

1. ✅ Add guardrails and tests for low-level grid builders (`build_rate_grid` and spread-grid edge cases).
2. ✅ Introduce adaptive boundary follow-up run (auto-expand grid when winner hits edge) before accepting recommendation.
3. ✅ Clarify and document objective economics, especially interaction between wholesale funding cost and shortfall penalty.
4. ✅ Add reproducible setup guidance for restricted-network environments (wheelhouse/constraints/local build instructions).

### Should improve next — completed

1. ✅ Remove duplicated grid-construction code into single shared helper (`grid_utils.py`).
2. ✅ Add scenario tests covering monotonic objective corner-cases and tie-breaking behaviour.
3. ✅ Add richer sensitivity framework (two-way stresses: elasticity × funding, competitor × funding).
4. ✅ Persist baseline/sensitivity outputs to versioned artifacts (`write_artifacts` in `runner.py`).

### Remaining open items

- ⬜ Fix example `sys.path` hack (low priority).
- ⬜ Add dedicated test for adaptive boundary expansion + re-check path.
- ⬜ Formalise deposit funding ratio interpretation (hard target vs soft risk appetite) in governance docs.

### Future enhancements

1. Move from single-price portfolio optimization to segmented pricing strategy constraints.
2. Add behavioral tenor/runoff and multi-period balance-sheet dynamics.
3. Add capital consumption / RWA / hurdle-rate terms and governance constraints.
4. Replace purely synthetic DGP with empirical calibration + challenger models + uncertainty-aware optimization.
