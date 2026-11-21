# Economic invariant testing (TEST-002, Phase 3c)

Property-based and metamorphic tests that check economic, optimisation and governance
invariants over generated synthetic portfolios. High line coverage alone did not catch
FIN-002, OPT-001, OPT-002 or OPT-003; these tests target those defect classes directly.
Line coverage is not evidence of economic correctness.

## Layout

| File | Invariants |
|---|---|
| `tests/unit/economic_strategies.py` | Shared Hypothesis strategies, run-mode settings, independent reference calculations |
| `tests/unit/test_invariants_response.py` | Loan/deposit partial effects (A-I), reference response, separability |
| `tests/unit/test_invariants_funding_objective.py` | Funding F01-F13, objective O01-O10 |
| `tests/unit/test_invariants_optimisation.py` | Single-tier segmented = joint (D01), multi-tier S01-S12 |
| `tests/unit/test_invariants_multiperiod.py` | Held-book cohort invariants M01-M20 |
| `tests/unit/test_invariants_governance.py` | ROE selection and gates G01-G12, policy band/boundary P01-P10 |
| `tests/unit/test_invariants_validation.py` | Finite-input and domain validation (auto-discovered config leaves) |
| `tests/unit/test_invariants_historical_anchors.py` | Portable OPT-001/OPT-002/FIN-002 anchors (stable public API only) |
| `tests/mutation/` | Runtime mutants and the expected-failure runner (never collected by pytest) |
| `docs/audits/2026-11-21-phase-3c-evidence/phase_3c_historical.py` | Runs the anchors at pre-fix commits in disposable worktrees |

All new tests carry the `economic_invariants` marker.

## Methodology

- Metamorphic: hold everything fixed, change one input, assert only the documented
  direction (e.g. higher loan rate never raises acceptance). Loan income and profit are
  deliberately *not* asserted monotone in rate; a guard test shows they are not.
- Independent references: `reference_*` functions restate `modelling_assumptions.md` and
  never call production pricing code.
- Fixed-candidate sensitivities (O04-O06) re-evaluate the same rate pair; no test assumes the
  optimiser's chosen rate moves monotonically with a parameter.
- Brute force: multi-tier problems are capped at 3 rates x 3 tiers per side (<= 729
  combinations).

## Valid input domains

Rates 0-15%; loan elasticities in [-400, 0], deposit in [0, 400]; funding ratio [0, 1];
penalty [0, 0.30]; runoff [0, 1] (endpoints weighted); discount [0, 0.20]; balances 0 or
$1-$5bn; RWA weight 0 or [0.01, 1]; hurdle [0, 0.25]. Generators build valid data directly
rather than filtering with `assume()`.

## Tolerances

Money uses relative `1e-9` and absolute NZD `1e-6` (`money_close`). Probabilities use
relative `1e-12` and absolute `1e-15`. Single-tier equivalence uses relative `1e-12`
and absolute `1e-9` for the decomposition, with exact rate equality. For exact ROE
floor comparisons the reference adds tier amounts in order with ordinary floating-point
addition. Python 3.12's compensated `sum()` can put an exact-floor candidate one ULP
away from the engine's result.

## Run modes

```bash
python -m pytest -m economic_invariants tests/unit -q                    # CI: derandomised
ECONOMIC_INVARIANT_SCALE=5 python -m pytest -m economic_invariants tests/unit   # stress
ECONOMIC_INVARIANT_EXPLORE=1 python -m pytest -m economic_invariants tests/unit --hypothesis-seed=123
python tests/mutation/run_mutation_checks.py --output <json>             # expected failures
python docs/audits/2026-11-21-phase-3c-evidence/phase_3c_historical.py --output <json>
```

Reproducing a failure: use the printed `@reproduce_failure(...)` blob, or in explore mode the
`--hypothesis-seed`. Note that Hypothesis mines literal constants from imported project
modules, so derandomised examples depend on the collected module set: running one file alone
can draw different examples from the full CI run. CI always collects the same tree.
Mutation runs use explicit and generated examples and skip shrinking. Each designated
detector must fail with an assertion. The historical runner verifies the specific
economic assertion on each pre-fix revision and all three anchors on current code.

## Annual credit loss verification (FIN-001, Phase 4b)

`test_credit_loss.py` verifies finite and bounded inputs, exact LVR endpoints,
missing LVR, acceptance-independent facility loss and EAD scaling. The
simulation tests check acceptance-weighted ECL, funding and interest on
principal, joint/segmented parity, profile lineage, governance and cohort
runoff. Validation tests include a controlled poor fit with predicted PD
37.5% versus observed 75%, AUC 0, and matched NZ$46 predicted versus NZ$30
realised. Sparse defaults and missing recoveries remain visible. The comparison
tests reconcile the fixed-offer and reoptimised results to their loss bands
and objective components. All inputs are synthetic.

```bash
python -m pytest -q tests/unit/test_credit_loss*.py
python -m scripts.run_calibration_baseline --output /tmp/phase-4a-baseline.json
cmp /tmp/phase-4a-baseline.json docs/audits/phase_4a_evidence/calibration-report.json
python -m scripts.run_credit_loss_comparison --output /tmp/phase-4b-comparison.json
cmp /tmp/phase-4b-comparison.json docs/audits/phase_4b_evidence/comparison.json
```

See the [model documentation](credit_loss_model.md) for the validation
population, metric definitions and approval requirements.

## Known limitations

- `portfolio_return_on_equity` overflows to `inf` when 0 < required capital < ~1e-290
  (a sub-dollar loan book). Unrealistic; a non-finite ROE cannot satisfy the floor
  disclosure, so the ROE gate warns rather than passes.
- A constant per-dollar capital cost rarely shifts a single-tier optimum by a grid step, so
  D01 seldom catches a capital-dropping search mutant; S01 (brute force) is its detector.
- These tests verify identities and documented behavioural assumptions of the *synthetic*
  model. They do not validate calibration or real-world economic realism (Phase 4).
