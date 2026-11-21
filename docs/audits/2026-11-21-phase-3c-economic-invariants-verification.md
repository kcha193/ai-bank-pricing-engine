# Phase 3c verification — economic invariants (TEST-002)

The November 13 audit found economic and governance defects despite 96.39% package
coverage. This suite checks model identities and decisions under generated changes to
synthetic portfolios. The production baseline is `cf8ffe2`, which includes the
segmented optimisation and cohort accounting fixes in PRs #78 and #79.

## Handoff recovery

Claude Code had prepared 69 pytest cases, shared Hypothesis strategies, run modes,
portable historical anchors, a mutation runner and preliminary evidence. The branch
had no Phase 3c commits, staged changes, stash or upstream PR. The marked suite and
Ruff passed on recovery. The pre-edit checkpoint preserved the whole handoff.

The continuation added ROE-aware expansion and both serialised relaxation states,
exercised 134 numeric config leaves in 1,608 rejection checks, and tied funding monotonicity to modelled
deposit uptake. It built joint candidate grids from the configured specs independently
of segmented output and checked carried rates against each origination decision.
The evidence runners now require economic assertion failures from the named tests.
The existing property suites and deterministic regressions remain part of verification.

## Invariant coverage matrix

The test module names below refer to `tests/unit/test_invariants_<module>.py` unless a
different path is given. The method column describes how expected results are obtained.

| Invariant | Tests | Production modules | Rationale and method | Defect evidence | Current result |
|---|---|---|---|---|---|
| Loan acceptance and expected volume fall or hold as the offered rate rises. Acceptance rises or holds as the competitor rate rises. | Response `test_r_l01_higher_loan_rate_never_raises_acceptance`, `test_r_l02_higher_competitor_loan_rate_never_lowers_acceptance` | `synthetic.response`, `pricing.profit` | Change one rate at fixed borrowers. Lower acceptance also lowers or preserves expected credit loss. Loan yield and volume can offset each other. | Loan competitor-gap sign mutant | PASS |
| TD uptake and volume rise or hold as the offer rises. Uptake falls or holds as the competitor rate rises. | Response `test_r_d01_higher_deposit_rate_never_lowers_uptake_volume_or_expense`, `test_r_d02_higher_competing_deposit_rate_never_raises_uptake` | `synthetic.response`, `pricing.profit` | Compare on the same depositor population. A rising non-negative rate times rising volume cannot lower interest expense. | TEST-002 partial effects | PASS |
| Zero elasticities drop the corresponding rate terms. Both probability functions are finite and bounded. | Response `test_r_l03_*`, `test_r_l04_*`, `test_r_d03_*`, `test_r_d04_*` and `test_response_properties.py::test_zero_*_elasticity_*` | `synthetic.response` | Recompute logistic probabilities independently from generated coefficients and customer attributes. | Loan competitor-gap sign mutant also exercises end-to-end response | PASS |
| Product responses are separable. Greater uptake cannot worsen the funding gap at fixed lending. | Response `test_r_x01_loan_and_deposit_responses_are_separable_on_the_joint_grid`, `test_r_d05_more_deposits_never_increase_shortfall_at_fixed_loans` | `optimization.grid_search`, `pricing.balance_sheet` | Alter the other product's offer or alter only the TD offer and carry the two expected deposit volumes into funding. | TEST-002 balance sheet partial effects | PASS |
| Wholesale funding, excess income, required deposits, shortfall and penalty each follow their own volume bases. | Funding and objective `test_f01_to_f06_funding_terms_match_hand_formulas_and_signs`, `test_f07_f08_shortfall_monotone_in_deposits_and_penalty_monotone_in_rate` | `pricing.profit`, `pricing.balance_sheet` | Use independent dollar differences and rates on fixed volumes. | Wholesale cost floor mutant | PASS |
| Zero funding ratio or penalty, `D = L`, `D = required deposits`, continuity, and positive scaling. | Funding and objective `test_f09_f10_*`, `test_f11_boundary_values_at_required_deposits_and_at_balanced_book`, `test_f12_*`, `test_f13_funding_terms_are_positively_homogeneous` | `pricing.profit`, `pricing.balance_sheet` | Funding has two distinct kinks. Scaling both sides by a positive constant scales all dollar terms. | TEST-002 funding edge cases | PASS |
| Complete objective at fixed joint and segmented candidates. | Funding and objective `test_o01_o10_joint_grid_matches_independent_end_to_end_objective`, `test_o02_segmented_portfolio_reconciles_to_tier_sums_and_reference` | `optimization.grid_search`, `optimization.segmented`, `diagnostics` | Compute each cash flow independently before subtracting booked costs, wholesale cost and penalty, and adding excess interest. | OPT-002 decomposition, loan competitor-gap sign mutant | PASS |
| Objective reacts dollar for dollar to ECL, capital and penalty changes. Zero volumes pay zero cash flows. Replication scales the objective and permutation preserves it. | Funding and objective `test_o04_*` through `test_o09_*` | `optimization.grid_search`, `optimization.segmented` | Compare the same candidate before and after changing its inputs. Compare portfolio values over the same grid when changing the population. | TEST-002 objective metamorphisms | PASS |
| One tier per product reproduces the joint optimiser across low and high penalties, deposit regimes and ROE policies. | Optimisation `test_d01_single_tier_segmented_equals_joint_optimiser`, historical anchor A2 | `optimization.segmented`, `optimization.grid_search` | From the configured specs, compare candidate grids, chosen rates, volumes, every objective component, ROE and relaxation. | OPT-002 pre-fix A2 failure and search penalty mutant | PASS |
| Small exact searches and uniform comparator, tie policy and grid expansion. | Optimisation `test_s01_s02_s03_s05_exact_search_matches_independent_brute_force`, `test_s04_segmented_is_never_worse_than_a_feasible_uniform_price`, `test_s07_solver_is_deterministic`, `test_s09_*`, `test_s12_*`. Existing `test_segmented_joint_optimisation.py::test_ties_break_on_lower_shortfall_then_enumeration_order` and `test_exact_ties_break_on_distance_to_market_reference_rates` | `optimization.segmented` | Score up to 729 candidates independently. Compare the feasible tier search with the uniform candidate and with contained grids. | OPT-002 search penalty and capital omission mutants | PASS |
| Coordinate descent respects ranking and reports convergence or cap. Exact solver discloses proven optimality. | Optimisation `test_s06_s11_coordinate_descent_moves_never_worsen_the_ranking_key`, `test_s11_iteration_cap_is_disclosed_not_reported_as_convergence`, `test_s01_s02_s03_s05_exact_search_matches_independent_brute_force` | `optimization.segmented` | Check accepted move keys, finite cap behaviour and solver status. | OPT-002 solver policy | PASS |
| Zero, full and partial runoff. Contractual rates, ECL rate and capital basis persist under drift. | Multiperiod `test_m01_to_m05_m18_*`, `test_m04_*`, `test_m06_to_m09_*`, `test_m06_m07_m20_*`, `test_m19_m20_*` | `scenarios.cohort_accounting`, `scenarios.multiperiod` | Use geometric survival, booked amounts pro rata, and each origination period's selected rates. Change market rates and compare booked terms. | FIN-002 cash-rate repricing, lost ECL and linear runoff mutants | PASS |
| Funding once on aggregate balances. Cohort order preserves economics. One cohort reconciles to the single-period objective and two identical cohorts double it. | Multiperiod `test_m10_m11_m17_active_book_equals_hand_objective_on_aggregate_balances`, `test_m12_identical_unrun_cohorts_double_every_term`, historical anchor A3 | `scenarios.cohort_accounting`, `scenarios.multiperiod` | Sum active volumes before valuing funding. Compare the no-runoff roll with twice the first period and check end-to-end NPV. | FIN-002 pre-fix A3 failure. Per-cohort funding, lost penalty, lost ECL and cash-rate repricing mutants | PASS |
| Discounted annual contributions equal NPV. New business plus incremental back book equals the active book. | Multiperiod `test_m13_m14_m15_m16_roll_reconciles_npv_discounting_and_attribution` | `scenarios.multiperiod` | Sum active objectives with independently calculated end-of-period discount factors. | FIN-002 roll and attribution | PASS |
| Feasible ROE floor enforced, infeasible floor disclosed, gate attached to the selected row. | Governance `test_g01_g02_g03_g05_selection_follows_constraint_first_ranking`, `test_g02_to_g05_g11_engine_roe_floor_is_met_or_disclosed`, historical anchor A1 | `optimization.grid_search`, `scenarios.runner`, `governance.gates` | Rank finite eligible candidates first and reconstruct gate evidence from the selected row. | OPT-001 pre-fix A1 failure. Silent relaxation and relaxed gate mutants | PASS |
| Non-positive objective or zero volume warns. Non-finite objective blocks. Block outranks warn. | Governance `test_g04_g06_to_g12_gate_statuses_follow_the_documented_policy` | `governance.gates` | Generate opposing gate outcomes and check priority and decision status. | OPT-001/GOV-002, negative objective gate mutant | PASS |
| Reject NaN, both infinities and invalid grids, elasticities, funding ratios and runoff. Keep valid zero edges. | Validation `test_v01_every_economic_number_rejects_nan_and_infinity_on_every_route`, `test_v02_*` through `test_v07_*` | `config`, `scenarios.runner`, `scenarios.cohort_accounting` | Exercise every discovered numeric config leaf with all three non-finite values through direct validation, YAML, copy/override and scenario entry. Generate invalid and valid domain edges separately. | OPT-003 validation regressions | PASS |
| Original policy band, initial and final boundary warnings, expansion cap, retained incumbent, and output disclosure. | Governance `test_p01_to_p09_adaptive_grid_discloses_band_breach_boundary_and_cap`, `test_p10_band_and_boundary_evidence_survive_summary_json`, `test_p_canonical_config_band_position_is_disclosed`. Existing `test_segmented_joint_optimisation.py::test_expansion_incumbent_survives_off_lattice_and_is_needed` | `diagnostics`, `optimization.segmented`, `scenarios.runner`, `governance.gates` | Recompute band breach and edges, re-rank original and expanded grids with the same ROE policy, and assert persisted fields with both relaxation states. | Policy band breach mutant | PASS |

## Numerical references and limits

Reference calculations in [economic_strategies.py](../../tests/unit/economic_strategies.py)
use the [model assumptions](../modelling_assumptions.md). The joint oracle derives loan
interest and expected loss and deposit expense from generated probabilities. The
segmented oracle sums candidate terms before applying portfolio funding and capital.
The cohort oracle applies runoff to booked balances and costs before funding the book.

Money uses a relative tolerance of `1e-9` and absolute NZD tolerance of `1e-6`.
The direct joint/segmented comparison uses `1e-12` relative and `1e-9` absolute on
components, with exact matching rates on a basis-point lattice. Response reference
probabilities use `1e-12` relative and `1e-15` absolute. ROE equality cases follow
the documented operation order at the boundary. Persisted rates and boolean disclosures
use exact comparisons. A seeded run exposed a one-ULP difference between the Python
3.12 compensated sum and the solver's left-to-right sum. The fixed three-tier example
pins the eligibility decision. The scalar reference uses the documented tier order
with plain floating-point additions.

The suite exercises synthetic model behaviour and small exhaustive grids. Coordinate
descent retains best-known status. The suite does not establish real-world calibration,
global optimality for coordinate descent, or behaviour for all IEEE-754 inputs. The
generators exclude positive sub-dollar balances in the standalone portfolio strategies
where vanishing capital can overflow ROE. See [run modes and limits](../economic_invariant_testing.md).

## Historical defects and controlled mutations

Use the project virtualenv from the repository root. The historical runner copies the
same portable tests into disposable, detached worktrees and verifies the import path.
The mutation runner makes a reversible in-process change in each isolated child pytest
process. Normal CI runs the unmutated suite with shrinking enabled. Mutation runs stop
each failed property at a counterexample.

```bash
python docs/audits/2026-11-21-phase-3c-evidence/phase_3c_historical.py \
  --output /tmp/phase3c-historical-final.json
python tests/mutation/run_mutation_checks.py --output /tmp/phase3c-mutants-final.json
```

| Defect | Pre-fix revision | Failing economic assertion | Corrected `cf8ffe2` |
|---|---|---|---|
| OPT-001 | `62b81d2` | A1 found a 161.81% floor above the maximum candidate ROE of 61.81%. The relaxation flag was missing. | A1 passed |
| OPT-002 | `3304b20` | A2 found a segmented loan rate of 4.99% and objective of −$1,922,874.85. Joint search chose 5.04% and −$1,868,034.78. | A2 passed |
| FIN-002 | `5141833` | A3 found a period-1 contribution of $2,420,511.37 with zero runoff, against twice the first period's $2,462,490.30. | A3 passed |

The pre-fix pytest processes returned 1. The corrected checkout returned 0 with all
three anchors passing. [Historical result details](2026-11-21-phase-3c-evidence/historical_results.json).

The fixed S01 cost example earns $0.20 less interest at the higher rate and saves
$0.16 each in capital and shortfall penalty. The complete objective chooses $3.84
over $3.72. Omitting either term reverses selection. The scalar oracle scores the
chosen result under the complete objective.

The clean mutation control passed all 69 cases. All 13 mutants failed all 20 designated
economic assertions. The omitted active-book penalty also tripped the new-cohort
reconciliation guard in two other cases. The runner records those secondary runtime
failures separately from the designated assertion. The child runs totalled 162.8 seconds.
[Mutation result details](2026-11-21-phase-3c-evidence/mutation_results.json).

## Offline verification

```bash
python -m ruff check .
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
python -m pytest -q -m 'not live_rates and not economic_invariants'
python -m pytest -q -m 'not live_rates'
python -m pytest --cov=balance_pricing_engine --cov-report=term-missing \
  --cov-fail-under=95 -m 'not live_rates' -q
python -m pytest -q -m economic_invariants --hypothesis-show-statistics
python -m pytest -q tests/unit/test_invariants_*.py \
  tests/unit/test_{objective_properties,response_properties,segmented_joint_optimisation,multiperiod,financial_input_validation,governance_gates}.py
for seed in 123 20261121 8675309; do
  ECONOMIC_INVARIANT_EXPLORE=1 python -m pytest -q -m economic_invariants --hypothesis-seed="$seed"
done
ECONOMIC_INVARIANT_SCALE=5 python -m pytest -q -m economic_invariants --hypothesis-show-statistics
```

Ruff passed. The 69 invariant cases and 272 existing regression cases passed
together in 15.03 seconds.

| Offline run | Passed | Failed | Skipped | Package coverage | Wall time |
|---|---:|---:|---:|---:|---:|
| Existing tests only | 3,581 | 0 | 2 | — | 224.69 s |
| Complete suite | 3,650 | 0 | 2 | — | 213.77 s |
| Complete suite with 95% coverage gate | 3,650 | 0 | 2 | 97.07% | 304.04 s |

The local full-suite timings varied with concurrent load. The marked suite gives
a direct measurement of the added tests. Both full runs deselected live-rate cases.

The marked suite collected 69 cases, including 52 Hypothesis properties. The normal
run completed 5,808 generated examples and 3 explicit examples in 15.79 seconds.
The three seeded runs passed in 12.33–15.23 seconds. The 5× stress run passed with
28,868 generated examples plus the same 3 explicit examples in 50.52 seconds.
These timings are local measurements on macOS arm64 with Python 3.12.13,
pytest 9.2.0 and Hypothesis 6.170.15.
