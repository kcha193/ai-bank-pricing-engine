# Pricing Engine Review — Implementation Backlog

> Source: end-to-end principal-ML review, 2026-05-04. Each item is a PR-sized task. Risk references (R##) and test references (T##) point back to the review summary in conversation.
>
> Suggested order: A1 → A2 → A3 → B1 → B2 → C1 → D1 → C2 → D2 → D3 → B3 → B4 → E1 → E2 → C3 → F1 → E3 → F2.

## Status legend

- [ ] not started
- [~] in progress
- [x] done

---

## Stage A — lock current behaviour

### [x] A1. Baseline golden test
**Goal:** Lock numeric `summary.json` for `run_baseline_diagnostics(load_config("configs/base.yaml"))`.

- New: `tests/unit/test_baseline_golden.py` (covers T4).
- Assert each `summary.json` field to a numeric tolerance (1e-6).
- Add `pytest --update-golden` fixture flag for intentional refreshes.
- Files: 1 new test file, no source changes.
- Risks addressed: R1, R25.

### [x] A2. Property tests for response + objective
**Goal:** Hypothesis-style invariants beyond the baseline.

- New: `tests/unit/test_response_properties.py` (T1) — probabilities ∈ [0, 1] for arbitrary rates and customer draws.
- New: `tests/unit/test_objective_properties.py` (T3, T20) — decomposition reconciles for arbitrary inputs; zero-elasticity closed form holds.
- Add `hypothesis` to dev deps in `pyproject.toml`.
- Risks addressed: R24.

### [x] A3. Tie-break policy
**Goal:** Make optimiser tiebreaker explicit and documented.

- Edit `select_best_result` in `src/balance_pricing_engine/optimization/grid_search.py`: ties resolve to lowest `funding_shortfall`, then lowest `|home_loan_spread_to_market_bps| + |term_deposit_spread_to_market_bps|`.
- Update `docs/model_governance.md` with the policy.
- Test: T14 — two genuinely tied rows differing in funding shortfall.
- Files: 1 src, 1 doc, 1 test extension.
- Risks addressed: R3.

---

## Stage B — explainability + boundary diagnostics

### [x] B1. Per-cause boundary warning + expansion-cap flag
**Goal:** Replace static cause text with diagnostic heuristic; flag when expansion was capped.

- Replace `BOUNDARY_WARNING_CAUSES` static tuple in `src/balance_pricing_engine/diagnostics.py` with a heuristic that inspects the winning `ObjectiveDecomposition`: dominant gradient (income vs penalty), competitor gap magnitude, funding-ratio proximity.
- Add `expansion_capped: bool` to `AdaptiveGridResult`, `ScenarioDiagnostics`, and `summary.json`.
- Tests: T5 (cap path) + new test for the heuristic categorisation.
- Files: `diagnostics.py`, `scenarios/runner.py`, tests.
- Risks addressed: R12, R13.

### [x] B2. Spread-policy band breach flag
**Goal:** Surface when adaptive expansion pushed the winner outside the user's configured spread-bps band.

- Track the original configured spread-bps band; emit `policy_band_breached: bool` on summary.
- Test T6.
- Files: `diagnostics.py`, `scenarios/runner.py`, tests.
- Risks addressed: R14.

### [ ] B3. Counterfactual decomposition
**Goal:** Reviewer-grade "what if we held the runner-up rate fixed" view.

- New method `ScenarioDiagnostics.counterfactual_decompositions(top_n: int = 3)`: for each of the top N rate pairs, hold one rate fixed, re-optimise the partner, return decompositions.
- Persist as `counterfactuals.csv` artefact.
- Test T17.
- Files: `diagnostics.py`, `scenarios/runner.py`, test.
- Risks addressed: R30.

### [ ] B4. Cohort attribution
**Goal:** Show which customer segments drove the chosen rates.

- New method `ScenarioDiagnostics.cohort_attribution()` returning loan-volume share by `risk_segment` and deposit-volume share by `wealth_segment` at the winning rates.
- Persist as `cohort_attribution.csv` artefact.
- Test T18.
- Files: `diagnostics.py`, runner, dataset segment helpers reuse.
- Risks addressed: R31.

---

## Stage C — pricing objective correctness

### [x] C1. Hoist deposit pre-compute (perf, byte-equal)
**Goal:** ~10–20× speedup on the inner loop with byte-equal output.

- Refactor `evaluate_joint_rate_grid` in `src/balance_pricing_engine/optimization/grid_search.py` to compute `deposit_probs / volume / cost` once per `deposit_rate` outside the loan loop.
- Test T16 — byte-equal grid_results vs prior implementation under the baseline config.
- Files: `grid_search.py`, test.
- Risks addressed: R34.

### [ ] C2. OCR offset on excess deposits
**Goal:** Stop charging excess deposits as pure cost.

- When `deposit_volume > loan_volume`, credit the excess at `cash_rate` (or a configurable `surplus_investment_rate`).
- Either net into `expected_wholesale_funding_cost` or add an `excess_deposit_income` line in the decomposition (prefer the latter for transparency).
- Update `OptimizationResult`, `ObjectiveDecomposition`, `summary.json` keys.
- Refresh the A1 golden in the same PR with note in `model_governance.md`.
- Files: `pricing/profit.py`, `grid_search.py`, runner artefacts, golden test refresh, model-card update.
- Risks addressed: R7, assumption #7.

### [ ] C3. Remove or wire-in `expected_loan_profit`
**Goal:** Eliminate divergent dead-code formulation.

- Recommended: delete `expected_loan_profit` from `src/balance_pricing_engine/pricing/profit.py`.
- Adapt `tests/unit/test_profit_and_balance_sheet.py::test_expected_credit_loss_reduces_loan_profit` to use the live decomposition.
- Test T15.
- Files: `pricing/profit.py`, `test_profit_and_balance_sheet.py`.
- Risks addressed: R4.

---

## Stage D — config + governance hygiene

### [ ] D1. Move response coefficients to YAML + model card
**Goal:** Surface hard-coded intercepts/anchors/feature coefficients.

- Add `response.coefficients:` block to `configs/base.yaml` (loan: intercept, rate_anchor, risk_score_coef, credit_score_coef, lvr_coef; deposit: intercept, rate_anchor, rate_sensitivity_coef, relationship_coef, digital_coef).
- Add `ResponseCoefficientsConfig` Pydantic model in `src/balance_pricing_engine/config.py`.
- Update `loan_acceptance_probability` / `deposit_uptake_probability` in `src/balance_pricing_engine/synthetic/response.py` to read from config.
- New: `docs/response_model_card.md` (sources, calibration window, validation, known limits, owner).
- Tests T13 + extensions to `test_response.py`.
- Refresh A1 golden if numeric path changes.
- Risks addressed: R5, R6, R29.

### [ ] D2. Business-sanity config validators
**Goal:** Catch silent gotchas at load time.

- In `src/balance_pricing_engine/config.py`, add `model_validator` checks (errors or warnings per item):
  - Negative `wholesale_funding_spread` → warn.
  - `funding_shortfall_penalty_rate < wholesale_funding_spread` → warn ("penalty dominated").
  - `n_loan_customers` or `n_deposit_customers` < 1000 → warn (high MC variance).
  - Configured spread band width < 50 bps → warn (likely to expand past policy).
- Tests T9, T10.
- Files: `config.py`, test additions.
- Risks addressed: R20, R21, R22, R23.

### [ ] D3. Competitor-rate `apply` integration test
**Goal:** End-to-end confirmation that `competitor_rates.enabled: true` actually mutates engine market state.

- New: `tests/unit/test_competitor_rate_apply_engine.py` (T8). Write fake `latest.json`, set `competitor_rates.enabled: true`, run `run_baseline_diagnostics`, assert in-engine competitor rates match the file and grids rebuild accordingly.
- Files: 1 new test, no source changes (apply.py already unit-tested).
- Risks addressed: R28.

---

## Stage E — sensitivity & explainability

### [ ] E1. Delta-from-baseline columns + parameter-type tag
**Goal:** Tornado-style sensitivity output.

- Extend `run_sensitivity_analysis` in `src/balance_pricing_engine/diagnostics.py` to add `delta_loan_rate_bps`, `delta_deposit_rate_bps`, `delta_objective_pct`, and `parameter_family` column ("elasticity" / "funding" / "competitor" / "wholesale").
- Update `src/balance_pricing_engine/reporting.py` to render the tornado.
- Test T12.
- Files: `diagnostics.py`, `reporting.py`, tests.
- Risks addressed: R16, R19, R32.

### [ ] E2. Stress competitor-gap elasticities + customer-feature coefficients
**Goal:** Close the gap-elasticity and feature-coef stress-test holes.

- After D1 lands (so feature coefficients are in config), extend `build_default_sensitivity_cases` to perturb the four elasticities AND a couple of customer-feature coefficients (e.g. `risk_score_coef`, `rate_sensitivity_coef`) by ±25%.
- Files: `diagnostics.py`, tests.
- Risks addressed: R7, R18.

### [ ] E3. Optional Monte Carlo over seeds
**Goal:** Capture customer-cohort uncertainty for governed comparisons.

- Extend signature: `run_sensitivity_analysis(..., n_seeds: int = 1)`. For `n_seeds > 1`, regenerate customer cohorts under multiple seeds and emit mean + standard error per case.
- Default stays `1` to preserve A1's golden.
- Files: `diagnostics.py`, `scenarios/runner.py`, tests.
- Risks addressed: R17.

---

## Stage F — modelling layer cleanup (lower urgency)

### [ ] F1. Refactor `agentic.py` to one training pipeline
**Goal:** Eliminate dual-API confusion for model-risk reviewers.

- Collapse `train_and_recommend` and `train` in `src/balance_pricing_engine/modelling/agentic.py` into a single `train(*, dataset, candidates, registry, ...)` returning a `TrainingResult` with optional report.
- Remove `_StaticProbabilityModel` proxy by splitting `evaluate_binary_classifier` in `src/balance_pricing_engine/modelling/evaluation.py` into:
  - `evaluate_probabilities(y_true, y_probability)` — pure metrics.
  - `evaluate_model(model, frame, features, target)` — thin wrapper around the first.
- Test T19 + update `tests/unit/test_modelling_agentic.py`.
- Files: `modelling/agentic.py`, `modelling/evaluation.py`, tests.
- Risks addressed: R33, R35, R36.

### [ ] F2. Document "ML diagnostic only" boundary
**Goal:** Doc-only banner for reviewers reading the modelling layer.

- Add a docstring to `src/balance_pricing_engine/modelling/__init__.py` stating "trained models are NOT consumed by the optimiser today" with a link to `docs/model_governance.md`.
- No tests required.

---

## Out-of-scope follow-ups (parked, not backlogged)

These came up during review but are larger than a PR or depend on a product decision:

- Personalised / segmented pricing (current scope is portfolio-level by design).
- True FTP curve, tenor mismatch, basis risk in funding cost.
- Capital / RWA / Tier-1 cost-of-equity term in the objective.
- Multi-period NPV with behavioural maturity / prepayment / runoff.
- IFRS 9 staging / PD-LGD-EAD decomposition for credit loss.
- Multiple-competitor distribution rather than single scalar competitor rate.
- Smooth shadow-cost funding penalty (replace hockey-stick with quadratic or log-barrier).

Open these as design-spec tickets when business priorities require them.
