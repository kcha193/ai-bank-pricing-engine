# Model Governance Notes

This project is a synthetic retail bank balance-sheet pricing prototype. It is not a production
pricing approval system.

## Current Model Boundary

The current engine optimizes one portfolio-level home loan rate and one portfolio-level term
deposit rate. It does not perform personalized pricing, customer-level discrimination testing,
capital allocation, or production limit management.

The response, credit-loss, and funding assumptions are deterministic code-level assumptions by
default. The ML response models train on synthetic outcomes generated from the known response
functions and help check whether learned models recover sensible rate-response direction.

An ML response model can influence optimization only through the explicit governed registry path:
`run_baseline_diagnostics(registry=...)` asks `ModelScoringAgent` for an approved compatible
champion and receives an `MLResponseFnAdapter` only when the champion is approved and its artifact
exists. If no approved compatible champion is available — no champion at all, or one that is not
yet approved — the runner falls back to the deterministic synthetic response functions.

Fallback is deliberately restricted to those benign cases. If an *approved* champion is selected
but its artifact is missing or cannot be loaded, that is an inconsistent governed state: the
registry promises an approved model that cannot actually set prices. The runner raises
`GovernanceError` rather than silently repricing on the synthetic fallback, so a pricing run can
never be recorded as "used approved champion X" while actually having used synthetic. Repair the
registry/artifact or retire the champion before pricing. This path is not enabled by default and
does not remove the portfolio-level, synthetic-data, human-review boundary of the prototype.

## Calibration Expectations

Before any production use, replace synthetic assumptions with governed calibration inputs:

- customer response elasticities estimated from controlled historical or experimental data
- credit-loss assumptions aligned to approved PD/LGD/EAD or expected-loss frameworks
- transfer-pricing and wholesale-funding assumptions aligned to treasury curves
- deposit-funding ratio and penalty parameters approved as explicit risk appetite settings

Each calibration update should record source data, sample window, transformations, validation
results, approval owner, and effective date.

## Canonical Funding Economics Assumption

Wholesale funding cost and funding shortfall penalty are intentionally additive in the current
objective. This is a modelling assumption for management analysis, not an accounting identity.

Wholesale funding cost represents the base marginal cost of externally funded lending balances:
when expected loan volume exceeds expected deposit volume, the unfunded balance is charged at the
wholesale funding rate.

When expected deposit volume exceeds expected loan volume, the surplus balance is credited at the
cash rate as `excess_deposit_income`. This keeps excess deposits from being treated as pure
interest expense while preserving the transparent component-level objective.

The funding shortfall penalty is a separate shadow cost for falling short of the target
deposit-funding ratio. It represents management's risk appetite for deposit-funded lending and can
be interpreted as a liquidity, franchise, or balance-sheet steering penalty. It does not replace
the wholesale funding cost unless a future model version explicitly changes the objective and
documents that policy choice.

## Deposit Funding Ratio — Soft Signal vs Hard Constraint

The `required_deposit_funding_ratio` parameter is a *soft risk appetite signal*, not a hard constraint. The optimizer penalises funding shortfalls via `funding_shortfall_penalty_rate` to steer recommendations toward the target, but a solution that falls short of the ratio may still be returned if the penalty does not fully outweigh the profit from higher loan volumes.

Callers who require a hard floor must either (a) set `funding_shortfall_penalty_rate` high enough to make shortfall solutions non-competitive, or (b) post-filter results by `funding_shortfall == 0`. This distinction should be noted in governance sign-off for each pricing run.

## Optimization Diagnostics Governance

Adaptive boundary remediation starts from the configured rate grids. If the first selected result
lands on a minimum or maximum grid boundary, the hit boundary is expanded and the grid search is
rerun until the result moves inside the searched domain or the maximum expansion count is reached.

Review the boundary fields together:

- `initial_boundary_warning_flag`: whether the first pass on the configured grid selected a rate
  on a grid boundary.
- `expansion_rounds`: how many adaptive expansion rounds were actually run before accepting the
  reported result.
- `boundary_warning_flag`: whether the final accepted result still sits on the final searched
  boundary after expansion.

A final `boundary_warning_flag` of `True` means the optimum may still lie outside the searched
domain. Treat that as unresolved model-risk evidence, not as fully remediated by the adaptive
follow-up. The appropriate response is governance review of the grid range, elasticities, funding
parameters, and competitor assumptions before relying on the recommendation.

Exact objective ties are resolved deterministically by a governed tie-break policy. The optimizer
first chooses the tied row with the lowest `funding_shortfall`. If funding shortfall also ties, it
chooses the tied row with the lowest combined absolute market spread:
`|home_loan_spread_to_market_bps| + |term_deposit_spread_to_market_bps|`. Remaining ties retain the
stable grid-search ordering.

Sensitivity cases also run adaptive grid expansion independently. Each sensitivity result is
optimized over its own final searched domain, so objective values should be compared alongside
`expansion_rounds`, `initial_boundary_warning_flag`, `boundary_warning_flag`, and the final
optimized rates. A case that required more expansion, or still ended with a boundary warning, is
not directly comparable to a clean interior optimum without that qualification.

## Validation Cadence

For prototype development, run the test suite and examples after pricing-model changes:

```bash
python3 -m pytest -q
python3 -m ruff check .
python3 examples/run_baseline.py
python3 examples/train_response_models.py
```

For governed use, add scheduled validation that compares recommendations, volumes, objective
components, and sensitivity outputs against prior approved baselines. Use
`ScenarioDiagnostics.write_artifacts()` to persist grid results, top candidates, sensitivity
summaries, and the accepted recommendation.

`ScenarioDiagnostics.write_artifacts()` uses fixed filenames in the requested output directory:
`grid_results.csv`, `top_results.csv`, optional `sensitivity.csv`, and `summary.json`. Reusing the
same directory overwrites those files. Use run-specific output directories, such as a dated or
commit-specific path, when artifacts need to be reproducible experiment records.

## Key Model-Risk Checks

- Boundary behavior: final recommendations should not remain at expanded grid boundaries without
  review.
- Elasticity uncertainty: challenger elasticity assumptions should not reverse recommendations
  without explanation.
- Funding interpretation: wholesale funding cost and shortfall penalty follow the canonical
  additive modelling assumption above.
- Synthetic circularity: ML diagnostics trained on synthetic outcomes do not validate real customer
  behavior.
- Approved-champion path: ML response models can affect optimization only when explicitly supplied
  through a registry and approved champion; verify champion scope, artifact existence, and fallback
  behavior before interpreting results.
- Conduct scope: portfolio-level pricing does not remove the need for future fairness and conduct
  controls if the project moves toward segmented or personalized pricing.
