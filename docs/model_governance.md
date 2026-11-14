# Model Governance Notes

This project is a synthetic retail bank balance-sheet pricing prototype. It is not a production
pricing approval system.

## Financial inputs and LLM scenario patches (OPT-003, GOV-007)

Economic configuration rejects NaN and both infinities, including rates, response coefficients,
funding parameters, grids, tier bounds and multi-period assumptions. Loan own-rate and
competitor-gap elasticities must be ≤ 0; deposit elasticities must be ≥ 0. Zero is permitted.
Existing bounds remain; a finite negative wholesale spread still produces its existing warning.
Economic `model_copy(update=...)` calls revalidate. Direct and governed scenario entry points
also validate existing instances before pricing.

`llm.scenario_interface.ALLOWED_PATCH_PATHS` is the explicit allow-list:

- `random_seed` (a non-negative integer).
- `market.cash_rate`, `wholesale_funding_spread`, `competitor_loan_rate`,
  `competitor_deposit_rate`, `home_loan_reference_rate`, `term_deposit_reference_rate`.
- `balance_sheet.required_deposit_funding_ratio`, `funding_shortfall_penalty_rate`.
- Existing `multiperiod.loan_runoff_rate`, `deposit_runoff_rate`, `discount_rate`,
  `market_cash_rate_drift_bps`, `competitor_loan_rate_drift_bps`,
  `competitor_deposit_rate_drift_bps`, `reinvestment_spread_bps`, `loan_tenor_years`.

Every patch value must be a finite JSON number. Strings, booleans, nested objects, unknown or
malformed paths, duplicate paths and whole-section replacement are rejected. LLM patches cannot
change any elasticity or response coefficient, the ROE floor, model registry/approval/governance,
paths, secrets, collection settings, grids, cohort sizes or execution controls. An absent
multi-period section must be configured manually before its assumptions can be patched.
The whole batch must pass `EngineConfig` validation before review and again before application.
Application modifies a detached in-memory document; any failure returns no modified config.

All three application methods (`apply`, `apply_to`, `apply_patches`) now require a `PatchApproval`
alongside the existing affirmative boolean. A boolean alone is intentionally no longer sufficient.
After human review, `NLScenarioResult.approve(approver_id=...)` creates an immutable record with a
non-empty identifier, UTC approval timestamp, SHA-256 proposal hash and exact approved patches.
The immutable proposal and approval content must match at application; replacing reviewed content
invalidates approval. No code in this interface automatically approves a proposal.

When `audit_log_path` is configured, JSONL records separate `proposal_generated`,
`proposal_rejected`, `approval`, `application_succeeded` and `application_failed` events.
Application records carry the model, prompt/proposal hashes, approved paths/values, approver,
approval timestamp, validation outcome and error category. Success includes the resulting config
SHA-256. Approval is recorded when an application attempt presents a matching review record.
Each successful call emits one application-success event, only after full validation. Raw prompts,
raw provider/validation exceptions, credentials and environment dumps are not recorded; rejected
unsafe payloads are omitted. Approved values are numeric only.

Limits: this is caller-attested approval, not authentication or a digital signature. The host must
identify the human and protect the optional local audit file. Hashes bind patch content, not a
particular base config; application revalidates the supplied base. This is not a durable transaction
or replay-prevention system. Finite, correctly signed inputs can still be economically implausible;
the existing warnings and governance gates remain necessary. Pricing maths and run-ledger schemas
are unchanged. See [LLM usage](usage_guide.md#human-reviewed-llm-scenarios).

## Governance Control Plane (`governance/`)

The primitives below (registry approval, fail-loud artifact checks, boundary/policy-band
diagnostics) are unified per decision by an explicit governance layer:

- `governance.gates.evaluate_gates()` runs a fixed set of typed `GateResult` checks (config
  validity, competitor-rate freshness, model approval status, model artifact availability,
  model/feature compatibility, boundary warning, expansion-cap, policy-band breach, funding
  shortfall, lineage completeness, objective finiteness, objective sign, non-zero volume, ROE
  floor) and derives a `DecisionStatus`:
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

### Economic-validity gates (OPT-001, GOV-002)

Three WARN-level gates stop an infeasible, loss-making or degenerate scenario from receiving a
clean `RECOMMENDED` status. They never change the selected rates.

| Gate | PASS | WARN |
|---|---|---|
| `ROE_FLOOR_MET` | No floor configured, or the selected ROE is finite and meets the floor | The floor was relaxed (`roe_constraint_relaxed`), or the ROE evidence is missing, non-finite or below the floor |
| `OBJECTIVE_POSITIVE` | Finite objective above zero | Zero or negative objective, or a non-finite one (which `OBJECTIVE_FINITE` also BLOCKs) |
| `VOLUME_NONZERO` | Expected loan and deposit volumes are both finite and above zero | Either volume is zero, negative, non-finite or missing |

- **ROE-relaxed recommendation.** No candidate met `min_return_on_equity`, so the engine returned
  the highest-objective candidate anyway. It is a diagnostic answer to "what is the best price if
  the hurdle is ignored?", not a price that meets the hurdle. A reviewer must lower or justify the
  floor, widen the grid, or reject the result.
- **Negative objective.** Every evaluated candidate loses money under the synthetic model. The
  result is the least-bad price, not a profitable one.
- **Zero volume.** A rate that attracts no business is not a usable price. Zero loan volume also
  leaves ROE undefined.
- **No materiality threshold.** `VOLUME_NONZERO` tests for strictly positive, finite volume. Any
  materiality threshold needs a business justification and a policy change.
- **Synthetic limitation.** A PASS on `OBJECTIVE_POSITIVE` or `ROE_FLOOR_MET` means the synthetic
  model's objective or ROE clears the test. It is not evidence of real-world profitability or of
  a real capital return. The model is uncalibrated and for decision support only.

The gate outcomes, with their evidence, are recorded in `decision_record.json`. `summary.json`
gains one additive key, `roe_constraint_relaxed`. Every existing key keeps its meaning.

See [docs/adr/0003-governance-centred-decision-record.md](docs/adr/0003-governance-centred-decision-record.md)
for the design rationale and alternatives considered.

**Invariant preserved, not re-implemented:** `application.PricingService.run_scenario()` calls
`run_baseline_diagnostics(registry=...)` exactly as before. If an approved champion's artifact is
missing or unloadable, `GovernanceError` still propagates out of `run_scenario()` unchanged — it
is never downgraded to a `BLOCKED` `PricingDecision`. The same holds for an approved champion that
fails integrity or scoring-contract validation. An inconsistent governed model state must
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
passes every integrity and scoring-contract check (see *Approved-champion integrity* below). If no
approved compatible champion is available — no champion at all, or one that is not
yet approved — the runner falls back to the deterministic synthetic response functions.

Fallback is deliberately restricted to those benign cases. If an *approved* champion is selected
but its artifact is missing, cannot be loaded, or fails any integrity or scoring-contract check,
that is an inconsistent governed state: the
registry promises an approved model that cannot actually set prices. The runner raises
`GovernanceError` rather than silently repricing on the synthetic fallback, so a pricing run can
never be recorded as "used approved champion X" while actually having used synthetic. Repair the
registry/artifact or retire the champion before pricing. This path is not enabled by default and
does not remove the portfolio-level, synthetic-data, human-review boundary of the prototype.

## Approved-champion integrity (GOV-001, SEC-007)

Implementation: `modelling/champion_integrity.py` (checks), `modelling/persistence.py`
(hash-then-load), `ModelRegistry.approve_model` / `promote_champion` (approval) and
`ModelScoringAgent.resolve_response_fn` (resolution). One validator serves both moments.

### Approval-time checks

`approve_model(..., product="loan"|"deposit")` and `promote_champion(..., product=...)` refuse
to approve, and leave the registry file unchanged, unless all of these hold:

1. `product` is given, or already on the record, and is `loan` or `deposit`. It is never
   inferred from the model name. A `product` that conflicts with the recorded one is refused.
2. The record has an `artifact_path` that exists, and a `feature_columns` contract: a non-empty,
   ordered list of unique names.
3. The artifact bytes are read once and hashed (SHA-256). The model is deserialised from those
   same bytes.
4. The object has a supported prediction interface: `predict_probability` (the `ResponseModel`
   protocol) or `predict_proba` (sklearn). A dict, a class or any other object is refused.
5. The object declares `feature_names_in_` (and `n_features_in_` when present), and it equals the
   contract in count, names and order. A model trained on a bare NumPy array has no names, so its
   column order cannot be verified and it is refused.
6. The deterministic probe below passes.
7. On re-approval, the artifact still matches the recorded `artifact_sha256`. A changed artifact
   must be registered as a new version, not re-approved in place.

On success the record gains these additive fields: `product`, `artifact_sha256`,
`feature_names`, `n_features`, `prediction_interface` and `integrity_validation`
(`status`, `schema_version`, `validated_at_utc`, `probe`). Any earlier `approved_by` /
`approved_at_utc` is kept in `previous_approvals`, not overwritten silently. Approval raises
`ChampionValidationError`, a `ValueError`.

### Resolution-time checks

Before an approved champion may drive pricing, `resolve_response_fn(feature_columns, fallback,
product=...)` checks the following. The runner passes `product="loan"` / `"deposit"`.

1. The artifact exists.
2. The record carries `artifact_sha256`, `feature_names`, `n_features` and `product`.
3. The recorded product is `loan` or `deposit` and matches the requested product.
4. The recorded hash is a 64-character hex digest.
5. The recorded `feature_names` and `n_features` equal the requested contract.
6. The artifact bytes match the approved SHA-256. This is checked **before** deserialisation,
   and the model is built from the verified in-memory bytes, so nothing is re-opened between
   check and use.
7. Checks 4–6 from the approval list run again: interface, feature contract and probe.

Any failure raises `GovernanceError`. The fallback still applies, unchanged, when no compatible
champion exists or the selected champion is not approved. Resolution never writes to the
registry, so it never backfills a hash.

### Probe

The probe runs `MLResponseFnAdapter` exactly as pricing does, so it derives `rate_gap_to_competitor`,
`risk_segment` and `wealth_segment` the same way. It uses 200 synthetic customers of the product
from a private generator (seed `20261113`), so it never touches the pricing run's random stream.
It holds the competitor rate fixed (loan 5.5%, deposit 4.5%) and sweeps 15 offered rates (loan
3%–10%, deposit 1%–8%). For each rate, the predictions must be a 1-D array with one value per
customer, numeric, finite, and within [0, 1].

The mean probability across the sweep is then checked against tolerance `1e-6`, the same value
`modelling.evaluation.check_monotonicity` uses:

- **Loan:** no step may rise by more than the tolerance. **Deposit:** no step may fall by more
  than the tolerance. A violation is reported as an economically inverted response.
- Flat steps are allowed, so a model that saturates near 0 or 1 at the ends is not rejected.
- Across the whole sweep the mean must move the right way by more than the tolerance. A fully
  flat response means the direction cannot be established, so it fails. The probe does not
  claim a pass it cannot show.

### GovernanceError cases

| Case | Message fragment |
|---|---|
| Artifact missing | `has a missing artifact` |
| Legacy approval without integrity metadata | `missing required integrity metadata ... re-approval is required` |
| Recorded product invalid or different from the request | `recorded product` |
| Recorded hash malformed | `artifact_sha256 is not a 64-character hex digest` |
| Recorded feature contract different from the request | `recorded feature_names` / `recorded n_features` |
| Artifact bytes changed after approval | `artifact SHA-256 mismatch` |
| Corrupt or unloadable bytes | `artifact could not be loaded (<ExceptionType>)` |
| Loadable non-model object | `has no supported prediction interface` |
| Feature count, name or order mismatch | `feature count mismatch` / `feature name mismatch` / `feature order mismatch` |
| No verifiable feature names (for example NumPy-trained) | `declares no feature names` |
| Wrong shape, non-numeric, NaN/inf or outside [0, 1] | `prediction shape` / `non-numeric` / `non-finite` / `outside [0, 1]` |
| Scoring raised during the probe | `prediction failed during probe ... <ExceptionType>` |
| Inverted rate response | `economically inverted <product> response` |
| Flat rate response | `rate-response direction could not be established` |

Messages name exception types, not exception text or artifact bytes. `PricingService.run_scenario()`
lets the error propagate unchanged, and the API still translates it into the structured `503`.

### Lineage and gates

`ModelLineage` gains these additive fields: `artifact_sha256`, `feature_names`, `n_features`,
`integrity_check` and `prediction_check`. They also appear in `decision_record.json` and the
`/v1` response. They are `None` on the synthetic path. `MODEL_ARTIFACT_AVAILABLE[<product>]`
carries them as evidence. It returns `BLOCK` if lineage reports an approved champion without
verified integrity evidence. That can happen only if the registry changes between resolution and
lineage capture (see GOV-005), so such a run can never be `RECOMMENDED`.

### Legacy migration

Champions approved before this change have no integrity metadata. They fail closed with
`GovernanceError` until a human re-approves them. Historical records are never changed
automatically. To migrate one record:

1. Confirm that the artifact at `artifact_path` is the model that was originally approved. Use
   its training run, model card and metrics, because no earlier hash exists to compare against.
2. If it was trained on a NumPy array, or exposes no `feature_names_in_`, retrain it on a named
   DataFrame and register it as a new version.
3. Call `ModelRegistry.approve_model(model_name, approver, version=..., product="loan"|"deposit")`.
   This runs the full approval checks and records the hash and contract. The earlier approver is
   kept in `previous_approvals`.
4. Re-run the scenario and confirm `integrity_check="passed"` in the decision lineage.

The deterministic `ElasticityBaselineModel` cannot be approved as a pricing champion: it has no
feature names and its rate response is flat under the probe. This closes the J5 oracle case.

### Remaining model-risk limitations

- Hash verification protects the approved identity of an artifact. It does **not** make a
  joblib/pickle safe: loading still executes the pickle. Treat artifacts as trusted local assets
  only.
- The probe is a minimum bar, not a validation. It checks the mean response at one competitor
  rate over a fixed cohort. It does not check calibration, per-customer monotonicity, the
  competitor-gap elasticity (MOD-001), or behaviour outside the sweep (MOD-002).
- Approval does not yet enforce a distinct approver or validation evidence (GOV-003).
- Lineage is still re-read from the registry after pricing, not captured at resolution
  (GOV-005).
- Shadow evaluation (`governance/shadow.py`) and diagnostic `ModelScoringAgent.score()` still
  load unapproved artifacts without a hash check. Neither one sets prices.
- Registry writes are not atomic, and the registry file itself is not signed (GOV-008).

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
   `ModelRegistry.approve_model(model_name, approver, product=..., config_hash=...)`, which also
   runs the approval-time integrity checks above; for config-level
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
  through a registry and approved champion whose artifact hash, feature contract and probe are
  verified at resolution; verify champion scope, `integrity_check` lineage and fallback behaviour
  before interpreting results.
- Conduct scope: portfolio-level pricing does not remove the need for future fairness and conduct
  controls if the project moves toward segmented or personalized pricing.
