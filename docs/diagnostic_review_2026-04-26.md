# Diagnostic Review — 2026-04-26

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
- Pricing grids are market-relative spread grids by default (basis points around reference rates).

## 2) Setup and run diagnostics requested

### Command outcomes

1. `python3 -m pip install -e ".[dev]"`
   - **Failed in this environment**: pip could not download build dependency `setuptools>=64` due proxy/network restrictions.
2. `python3 -m pytest -q`
   - **Passed**: 33 passed.
3. `python3 -m ruff check .`
   - **Passed**: all checks passed.
4. `python3 examples/run_baseline.py`
   - **Passed** and produced baseline optimization output.
5. `python3 examples/train_response_models.py`
   - **Passed** and produced model metrics/calibration/sanity checks.

### Does the project run successfully?

- **Mostly yes**: tests, lint, baseline scenario, and model-training example run successfully.
- **Packaging/setup is currently environment-sensitive** because editable install depends on fetching build dependencies from network.

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
- Good control lever, but can overlap economically with wholesale funding cost and create double-penalization depending on interpretation.

### Objective function

- Clear and decomposable.
- In baseline output objective is materially negative with very large shortfall, suggesting configuration mismatch between demand, grids, and funding constraints rather than optimizer failure.
- No capital/RWA constraints, NII volatility, customer fairness, or volume growth targets.

### Optimizer grid design

- Exhaustive grid search is transparent and robust for small grids.
- Baseline winner lands on max boundary for both products: strong sign grid is too narrow and/or objective/elasticities produce monotonic push to boundaries.
- Grid resolution fixed at 5 bps; no local refinement around optima.

### Boundary diagnostics

- Existing diagnostics correctly warn when winner is at min/max boundary.
- Helpful explanatory text exists; next step is actionable auto-remediation (e.g., adaptive re-grid).

### Sensitivity analysis

- Includes one-at-a-time perturbations to elasticities, funding penalty/rate, and competitor rates.
- In current baseline all scenarios still hit upper boundaries, which weakens decision confidence (insufficiently informative search range).
- No multi-factor stress interactions.

## 4) Identified issues

## Bugs / defects

1. **Potential API-level grid bug**: `build_rate_grid` has no explicit validation for `step <= 0` or `stop < start` (unlike config validators), allowing misuse if called directly.
2. **Code duplication risk**: rate-grid construction logic exists in both `scenarios/runner.py` and `diagnostics.py`; behaviour can drift if one side changes.
3. **Example import hack**: examples modify `sys.path` manually. Works, but can mask packaging/import issues.

## Missing tests

- No direct unit tests for invalid `build_rate_grid` inputs (zero/negative step, reversed bounds).
- Limited tests around objective interactions (e.g., no-regression checks for wholesale cost + funding penalty combined effect).
- No tests asserting boundary-warning remediation workflow (because there is no adaptive re-grid yet).

## Unclear assumptions

- Whether wholesale funding cost and funding shortfall penalty are intentionally additive as distinct economics vs policy overlay.
- Interpretation of required deposit funding ratio as hard strategic target vs soft risk appetite metric.
- Annualized single-period treatment for both loans and deposits (no tenor dynamics).

## Model-risk concerns

- Synthetic-data circularity: optimization and ML diagnostics rely on same stylized latent model.
- Elasticities are point estimates without confidence bounds/priors.
- No challenger models for response, credit loss, or funding.
- No fairness/conduct constraints in pricing decisions.

## Documentation gaps

- No dedicated model-governance note describing parameter calibration, validation cadence, and approval process.
- No architecture/dependency diagram for engine flow (data generation -> response -> economics -> optimization -> diagnostics).
- No explicit troubleshooting section for offline/air-gapped installation.

## 5) Ranked action plan

### Must fix now

1. Add guardrails and tests for low-level grid builders (`build_rate_grid` and spread-grid edge cases).
2. Introduce adaptive boundary follow-up run (auto-expand grid when winner hits edge) before accepting recommendation.
3. Clarify and document objective economics, especially interaction between wholesale funding cost and shortfall penalty.
4. Add reproducible setup guidance for restricted-network environments (wheelhouse/constraints/local build instructions).

### Should improve next

1. Remove duplicated grid-construction code into single shared helper.
2. Add scenario tests covering monotonic objective corner-cases and tie-breaking behaviour.
3. Add richer sensitivity framework (two-way stresses: elasticity × funding, competitor × funding).
4. Persist baseline/sensitivity outputs to versioned artifacts for regression monitoring.

### Future enhancements

1. Move from single-price portfolio optimization to segmented pricing strategy constraints.
2. Add behavioral tenor/runoff and multi-period balance-sheet dynamics.
3. Add capital consumption / RWA / hurdle-rate terms and governance constraints.
4. Replace purely synthetic DGP with empirical calibration + challenger models + uncertainty-aware optimization.

