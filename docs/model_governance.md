# Model Governance Notes

This project is a synthetic retail bank balance-sheet pricing prototype. It is not a production
pricing approval system.

## Governance Control Plane (`governance/`)

The primitives below (registry approval, fail-loud artifact checks, boundary/policy-band
diagnostics) are unified per decision by an explicit governance layer:

- `governance.gates.evaluate_gates()` runs a fixed set of typed `GateResult` checks (config
  validity, competitor-rate freshness, model approval status, model artifact availability,
  model/feature compatibility, boundary warning, expansion-cap, policy-band breach, funding
  shortfall, lineage completeness, objective finiteness) and derives a `DecisionStatus`:
  `RECOMMENDED`, `REVIEW_REQUIRED`, or `BLOCKED`. Policy is conservative but not maximal: any
  `BLOCK` gate yields `BLOCKED`; otherwise any `WARN` yields `REVIEW_REQUIRED`; a clean gate set
  is `RECOMMENDED`. The underlying optimiser recommendation is preserved for `REVIEW_REQUIRED` —
  governance withholds the "safe to act on" label, it does not discard the result.
- `governance.lineage.resolve_model_lineage()` re-derives (read-only) which response source
  (deterministic synthetic vs. an approved ML champion) drove each product's pricing, for
  recording — it does not duplicate the `GovernanceError` fail-loud check, which still lives
  solely in `ModelScoringAgent.resolve_response_fn`.
- `governance.decision_record.DecisionRecord` packages the decision status, gates, model/feature
  lineage, boundary/policy-band flags, and stage timings into an **additive**
  `decision_record.json` artifact, written alongside (never replacing) the existing run-ledger
  files documented in [docs/run_ledger_guide.md](docs/run_ledger_guide.md).
- `governance.shadow.evaluate_shadow_challenger()` is a narrow, optional champion/challenger
  comparison: it scores the same synthetic feature sample with the current approved champion and
  the latest compatible, non-champion challenger, purely for observation. It never changes the
  selected rates, never bypasses champion approval, and never raises — a missing or unscoreable
  challenger is recorded as an `error` field on the result, not propagated.

See [docs/adr/0003-governance-centred-decision-record.md](docs/adr/0003-governance-centred-decision-record.md)
for the design rationale and alternatives considered.

**Invariant preserved, not re-implemented:** `application.PricingService.run_scenario()` calls
`run_baseline_diagnostics(registry=...)` exactly as before. If an approved champion's artifact is
missing or unloadable, `GovernanceError` still propagates out of `run_scenario()` unchanged — it
is never downgraded to a `BLOCKED` `PricingDecision`. An inconsistent governed model state must
fail loud, not be represented as a normal (if blocked) decision. The FastAPI `/v1/pricing/scenarios`
endpoint is the only place this exception is translated — into a `503` with a structured
`MODEL_ARTIFACT_UNAVAILABLE` error body, never a raw stack trace.

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

## Calibration Cadence and Approval Process

The list above defines *what* must be calibrated and *what evidence* each update records. This
section defines *when* parameters are reviewed, *who* approves a change, and *what gates* a
proposed value before it can drive a priced recommendation. It applies to the governed pricing
parameters carried in config — response elasticities (`response.loan_rate_elasticity`,
`response.loan_competitor_gap_elasticity`, `response.deposit_rate_elasticity`,
`response.deposit_competitor_gap_elasticity`) and response intercepts/feature coefficients
(`response.coefficients.loan.*`, `response.coefficients.deposit.*`); funding parameters
(`market.wholesale_funding_spread`, `balance_sheet.required_deposit_funding_ratio`,
`balance_sheet.funding_shortfall_penalty_rate`); and competitor/reference rates
(`market.competitor_loan_rate`, `market.competitor_deposit_rate`,
`market.home_loan_reference_rate`, `market.term_deposit_reference_rate`) — and to any ML champion
whose response function can replace the response parameters via the registry path.

Credit-loss assumptions are **not** currently a config field, so this process does not yet cover
them end to end: `expected_loss_rate` is generated per loan customer from `risk_score` by a
hardcoded formula in `synthetic/customers.py` and consumed in `pricing/profit.py`. Calibrating it
today means changing that code, not a config value, so it falls outside the `config_hash`/run-ledger
audit trail described below. Treat this as an open gap — either promote the credit-loss formula's
coefficients into `EngineConfig` before relying on this section for credit-loss sign-off, or track
credit-loss calibration through a separate, code-level review record until it is.

### Cadence

- **Periodic review (scheduled).** Re-validate every governed parameter on a fixed cycle —
  recommended quarterly for response elasticities and funding parameters, at least semi-annually
  for credit-loss assumptions, aligned to the cycle of the upstream PD/LGD/EAD and treasury-curve
  owners. A review may *reaffirm* the current value; reaffirmation is itself a recorded approval.
- **Event-triggered (off-cycle) review.** Recalibrate ahead of the scheduled cycle when any of the
  following occurs: a cash-rate or competitor-rate regime shift; a sustained breach between
  predicted and realised acceptance/uptake or loss outcomes; a final `boundary_warning_flag` that
  recurs across runs (see *Optimization Diagnostics Governance*); a change to the response
  functional form or feature schema; or a new champion proposed for promotion.

### Roles and approval workflow

Mirror the ML champion approval pattern already implemented in `ModelRegistry`
(`approve_model` / `promote_champion`), which records the approver and the `config_hash` the
approval is bound to:

1. **Proposer** assembles the evidence package (below) and the proposed parameter value(s).
2. **Independent reviewer** — not the proposer — checks the evidence against the acceptance gates.
3. **Approver** (model-risk owner) records sign-off. For an ML champion this is
   `ModelRegistry.approve_model(model_name, approver, config_hash=...)`; for config-level
   parameters it is a run-ledger governance-checklist sign-off
   (`sign_off_item` / `sign_off_all_items` in `run_ledger/checklist.py`) on the run that first
   uses the new value. No parameter change is "approved" until this record exists.

Separation of duties is mandatory: the proposer must not be the sole approver.

### Acceptance gates

A proposed value (or champion) is rejected unless **all** of the following hold:

- **Provenance complete** — every field in the *Calibration Expectations* evidence list is
  populated (source data, sample window, transformations, validation results, approval owner,
  effective date).
- **Directional sanity** — loan own-rate and competitor-gap elasticities stay negative; deposit
  elasticities stay positive; an ML champion passes the rate-response direction and monotonicity
  checks in `modelling/evaluation.py` before it is eligible for `approve_model`.
- **Baseline comparison** — re-run the canonical scenario and diff against the prior approved
  baseline artifacts (`ScenarioDiagnostics.write_artifacts()` into a run-specific directory). A
  recommendation reversal, a material objective/volume shift, or a *new* persistent
  `boundary_warning_flag` requires written justification, not silent acceptance.
- **Bounded, explained change** — large jumps in any single parameter are explained by the
  evidence, not curve-fitted to a desired price.

### Rejection and rollback

If the gates are not met, the change is rejected and the **prior approved value is retained** —
the default-safe state. A rejected ML champion is left unapproved, so the registry path falls back
to the deterministic synthetic response (see *Current Model Boundary*); it never silently reprices.
Record the rejection rationale alongside the proposal.

### Audit trail

Every priced run already records the `config_hash`, `git_commit`, and UTC `timestamp` in
`RunMetadata`, so the exact parameter set behind a recommendation is reconstructible. Approvals are
bound to that `config_hash` (registry) or to the run's governance checklist (config parameters),
giving a complete who-approved-what-when chain from approved parameter value to priced output.

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
