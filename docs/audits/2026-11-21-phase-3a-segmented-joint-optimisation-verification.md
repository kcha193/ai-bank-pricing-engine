# Phase 3a verification: segmented joint optimisation (OPT-002)

Date: 2026-11-21. Branch: `fix/phase-3a-segmented-joint-optimisation`. Finding: OPT-002 (P1)
(`2026-11-13-remediation-roadmap.md`, row 3a). Offline only: synthetic data, no live fetching,
no production writes.

## Root cause

`optimization/segmented.py` did not maximise the portfolio objective:

| Mismatch | Old behaviour |
| --- | --- |
| Loan tiers | Each tier maximised `income - ECL - capital - wholesale(volume, 0)`: every loan wholesale funded, no shortfall penalty, no deposit offset. |
| Deposit tiers | Chosen one at a time against the already-fixed loan volume; each tier's benefit depended on the deposit tiers chosen before it (`accumulated_deposit_volume`). |
| Objective | The full portfolio objective was computed only after all rates were fixed. |
| ROE floor | No constrained alternative: any segmented result below the floor was reported relaxed. |
| Ties | First strictly-greater rate per tier, not the joint order (objective, shortfall, spread distance, enumeration). |
| Expansion | Re-ran the greedy rule; an expanded grid could drop the previous winner (off-lattice stop). |

## Changes

- `optimization/segmented.py`: per-tier precompute (`precompute_*_tier_candidates`), canonical
  `evaluate_segmented_portfolio`, `solve_segmented_portfolio` (exact search up to 2,000,000
  combinations, always for one tier per side; otherwise deterministic multi-start coordinate
  descent), `run_coordinate_descent`, `SolverSettings` (bounded), `SolverDiagnostics`, uniform
  baseline safeguard, incumbent support for expansion.
- `pricing/profit.py`, `pricing/balance_sheet.py`: the portfolio funding terms accept candidate
  arrays elementwise (`non_negative_part`); scalar inputs return the same Python floats as before.
  `portfolio_return_on_equity` is the one ROE definition, now used by the joint grid search, the
  segmented solver and the segmented grid rows.
- `scenarios/runner.py`: passes the ROE floor, reference rates, solver settings and the previous
  round's tier rates; additive `segmented_solver` in `summary.json`; a single-tier result reports
  the tier rate exactly.
- `api/limits.py`: comment only (solver work is capped by `SolverSettings` ceilings).
- Docs: `modelling_assumptions.md` (new "Segmented tier pricing" section), `architecture.md`
  module map, `AGENTS.md` invariant.

Unchanged: elasticities, ECL, funding-rate and capital calibration, the single-period economic
formula, customer generation, the joint optimiser's selection, config schema and config hashing.

## Original audit reproduction (scenario A)

`2026-11-13-evidence/fin/followup.py` case 4 (unchanged script): one catch-all tier, loans 3-9%,
deposits 2-6%, 5 bp steps, `configs/base.yaml`, seed 42.

| | Loan | Deposit | Objective | Shortfall |
| --- | --- | --- | --- | --- |
| Audit (Nov 13) segmented | 5.65% | 4.20% | -$0.27m | $552m |
| Before (master 3304b20) segmented | 5.65% | 4.20% | -$269,144 | $552,407,254 |
| After segmented | 6.15% | 4.15% | +$5,904,280 | $2,363,489 |
| Joint optimiser (same grid) | 6.15% | 4.15% | +$5,904,280 | $2,363,489 |

After: segmented and joint rates, objective and every component are bit-identical (objective gap
0.0). The segmented result is no longer dominated by a feasible candidate on the same grid.

## Before/after comparison

`2026-11-21-phase-3a-evidence/phase_3a_comparison.py`, run against master
(`phase_3a_comparison_before_master_3304b20.json`) and this branch
(`phase_3a_comparison_after.json`). $m except rates and ROE.

| Scenario | Version | Tier rates (%) | Loans | Deposits | Loan int. | ECL | Capital | Dep. int. | Wholesale | Shortfall | Penalty | Excess dep. | Objective | ROE |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A audit one tier | before | L 5.65; D 4.20 | 1,429.64 | 376.86 | 80.77 | 10.32 | 6.00 | 15.83 | 38.95 | 552.41 | 9.94 | 0.00 | -0.27 | 11.5% |
| A audit one tier | after | L 6.15; D 4.15 | 559.67 | 361.42 | 34.42 | 3.79 | 2.35 | 15.00 | 7.34 | 2.36 | 0.04 | 0.00 | 5.90 | 42.1% |
| B two-by-two (81 combos) | before | L 6.00/5.50; D 4.30/4.00 | 1,427.56 | 377.32 | 79.39 | 9.83 | 6.00 | 15.94 | 38.86 | 550.60 | 9.91 | 0.00 | -1.15 | 9.7% |
| B two-by-two (81 combos) | after | L 6.50/6.00; D 4.30/4.00 | 647.44 | 377.32 | 39.08 | 4.15 | 2.72 | 15.94 | 9.99 | 43.52 | 0.78 | 0.00 | 5.48 | 36.2% |
| C production fixture | before | L 6.50/6.00/7.00; D 4.20/4.00/4.40 | 637.71 | 391.20 | 38.46 | 4.06 | 2.68 | 16.69 | 9.12 | 23.31 | 0.42 | 0.00 | 5.50 | 36.6% |
| C production fixture | after | unchanged | 637.71 | 391.20 | 38.46 | 4.06 | 2.68 | 16.69 | 9.12 | 23.31 | 0.42 | 0.00 | 5.50 | 36.6% |
| D high penalty (20%) | before | L 6.50/6.00/7.00; D 5.70/5.50/4.40 | 637.71 | 508.40 | 38.46 | 4.06 | 2.68 | 26.93 | 4.78 | 0.00 | 0.00 | 0.00 | 0.01 | 12.0% |
| D high penalty (20%) | after | L 7.00/6.00/7.00; D 4.20/4.50/4.40 | 612.22 | 401.66 | 36.84 | 3.83 | 2.57 | 17.27 | 7.79 | 0.00 | 0.00 | 0.00 | 5.37 | 37.1% |
| E low deposit supply | before | L 6.50/6.00/7.00; D 4.20/4.00/4.40 | 637.71 | 79.56 | 38.46 | 4.06 | 2.68 | 3.39 | 20.65 | 334.95 | 6.03 | 0.00 | 1.65 | 19.4% |
| E low deposit supply | after | L 6.50/6.50/7.00; D 4.20/4.00/4.40 | 222.81 | 79.56 | 14.50 | 1.43 | 0.94 | 3.39 | 5.30 | 65.27 | 1.17 | 0.00 | 2.26 | 41.0% |
| F excess deposits | before | L 6.50/6.00/7.00; D 4.20/4.00/4.40 | 637.71 | 3,146.62 | 38.46 | 4.06 | 2.68 | 134.19 | 0.00 | 0.00 | 0.00 | 62.72 | -39.74 | -166.1% |
| F excess deposits | after | unchanged | 637.71 | 3,146.62 | 38.46 | 4.06 | 2.68 | 134.19 | 0.00 | 0.00 | 0.00 | 62.72 | -39.74 | -166.1% |
| G adaptive expansion | before | L 5.50/5.50/5.50; D 4.20/4.00/4.30 | 1,730.83 | 382.18 | 95.20 | 12.75 | 7.27 | 16.15 | 49.90 | 742.86 | 13.37 | 0.00 | -4.24 | 5.0% |
| G adaptive expansion | after | L 6.50/6.00/6.50; D 4.20/4.00/4.30 | 647.44 | 382.18 | 39.08 | 4.15 | 2.72 | 16.15 | 9.81 | 38.66 | 0.70 | 0.00 | 5.54 | 36.5% |

| Scenario | Objective change | Method | Search space | Expansions | Final boundary warning | Policy band breached | Non-PASS gates (after) |
|---|---|---|---|---|---|---|---|
| A | +$6.17m | EXACT | 9,801 | 0 | no | no | FUNDING_SHORTFALL, LINEAGE_COMPLETE |
| B | +$6.63m | EXACT | 81 | 0 | no | no | FUNDING_SHORTFALL, LINEAGE_COMPLETE |
| C | $0 | EXACT | 6,400 | 0 | yes | no | BOUNDARY_WARNING, EXPANSION_CAPPED, FUNDING_SHORTFALL, LINEAGE_COMPLETE |
| D | +$5.36m | EXACT | 6,400 | 0 | yes | no | BOUNDARY_WARNING, EXPANSION_CAPPED, LINEAGE_COMPLETE |
| E | +$0.61m | EXACT | 6,400 | 0 | yes | no | BOUNDARY_WARNING, EXPANSION_CAPPED, FUNDING_SHORTFALL, LINEAGE_COMPLETE |
| F | $0 | EXACT | 6,400 | 0 | yes | no | BOUNDARY_WARNING, EXPANSION_CAPPED, LINEAGE_COMPLETE, OBJECTIVE_POSITIVE |
| G | +$9.79m | EXACT | 194,481 | 1 | no | yes | FUNDING_SHORTFALL, LINEAGE_COMPLETE, POLICY_BAND_BREACH |

No scenario got worse. C and F were already optimal under the old rule. Every governance status
is REVIEW_REQUIRED before and after. `LINEAGE_COMPLETE` WARNs in every row because the evidence
script passes no feature lineage, not because of this change. A and B lose their
`OBJECTIVE_POSITIVE` warning, and B and G lose boundary/expansion warnings, because the joint
optimum is interior and profitable. F stays loss-making: 40,000 deposit prospects with no funding
ratio is a deliberate excess-deposit stress, and its deposit tiers sit at the grid minimum.

## Single-tier equivalence

`test_single_tier_segmented_matches_joint_optimiser` covers five balance-sheet cases (base, 20%
penalty, zero ratio, zero penalty, 100% ratio). Loan rate, deposit rate, objective, both volumes,
loan interest, ECL, capital, deposit interest, wholesale cost, excess income, penalty and
shortfall are compared with `==` (bit-identical), along with ROE. A separate test places an ROE
floor between candidates. Both optimisers then select the same constrained pair, and neither
reports relaxation.

## Multi-tier search

- Exact search equals an independent brute force that calls the canonical scalar evaluation for
  every combination (two loan × two deposit tiers, three rates each: 81 combinations). It also
  finds a known global optimum that a tier-by-tier choice misses.
- The code reviewer's randomised probes compared exact search against brute force: 400 problems,
  1,500 more with 1-7-candidate blocks, and 1,000 single-tier cases against
  `select_best_result_with_disclosure`. These included ties, NaN distances and reachable and
  unreachable ROE floors. There were no mismatches.
- Coordinate descent: accepted moves strictly increase the objective (no floor). It reaches the
  exact optimum on a separable fixture, respects `max_iterations` and reports non-convergence. It
  is deterministic, and each start's final objective is ≥ its initial objective. Under an ROE
  floor the (meets floor, objective) key never decreases. Gaining the floor may lower the objective
  (tested).
- Uniform baseline: feasible only with a rate shared by every tier on each side. The segmented
  objective is ≥ the uniform baseline for both methods, including when `max_starts=1` truncates
  the uniform start: a start stalled at $194,130 against a $394,497 uniform baseline, and the
  uniform portfolio stays admissible. No dominance claim is made when the grids are disjoint.

## Boundary expansion

The previous round's tier rates join the expanded candidate sets and seed descent. Regression
test: loan tiers 5.00-6.20% in 0.5% steps (6.20% appended off-lattice) win at 6.20%. The expanded
grid (5.00, 5.50, ..., 7.00, 7.40%) drops 6.20%. Without the incumbent the expanded objective
falls ($348,392 < $350,884); with it the objective is kept ($350,884). Initial/final boundary
diagnostics, expansion counts, the cap and `policy_band_breached` keep their meaning (scenario G).

## Performance

`2026-11-21-phase-3a-evidence/phase_3a_benchmark.py` (`.out`): Apple arm64, Python 3.12.13, best
of three runs, portfolio search only.

| Case | Method | Combinations | Seconds | Objective |
|---|---|---|---|---|
| 1×1 tiers, 121×81 rates | EXACT | 9,801 | 0.0002 | 1,272,935 |
| 2×2 tiers, 32 rates | EXACT | 1,048,576 | 0.0066 | 3,030,693 |
| 2×2 tiers, 32 rates | DESCENT | 1,048,576 | 0.0019 | 3,025,548 (-0.17%) |
| 3×3 tiers, 11 rates | EXACT | 1,771,561 | 0.0108 | 4,294,083 |
| 3×3 tiers, 11 rates | DESCENT | 1,771,561 | 0.0025 | 4,281,540 (-0.29%) |
| 3×3 tiers, 61 rates | DESCENT | 5.2e10 | 0.0053 | 4,410,382 |
| 5×5 tiers, 121 rates | DESCENT | 6.7e20 | 0.0225 | 7,840,012 |

Peak memory for about two million exact combinations was 22-76 MB in 250,000-candidate blocks
(161 MB with 1,000,000-candidate blocks), measured with `tracemalloc`. End-to-end
`configs/segmented_pricing.yaml` took 0.008 s after and 0.009 s before. The audit scenario took
0.024 s after and 0.023 s before. The default exact threshold of 2,000,000 follows from the gap
between descent and exact search.

## Tolerances

- Coordinate move: improvement > `max(1e-6, 1e-12 × |objective|)` dollars, or newly meeting the
  ROE floor.
- Ties: exact float equality, as in the joint optimiser.
- Single-tier equivalence: exact equality, no tolerance.
- Ceilings: exact 10,000,000 combinations, 1,000 passes, 32 starts, 4,000,000-candidate blocks.

## Verification

- `python -m ruff check .`: clean.
- `python -m pytest --cov=balance_pricing_engine --cov-report=term-missing --cov-fail-under=95 -m "not live_rates" -q`:
  3,540 passed, 2 skipped (environment-dependent, pre-existing; one is the Playwright
  missing-dependency path), total coverage 96.99% (`segmented.py` 99%).
- New tests: `tests/unit/test_segmented_joint_optimisation.py`.
- `tests/unit/test_segmented_pricing.py`: the two tests that called the removed
  `_optimize_deposit_tier` now check the same economics through the joint solver.

## Remaining risks

- Coordinate descent above 2,000,000 combinations is a heuristic. It was 0.2-0.3% below the
  optimum on the benchmarks and is reported as best-known (`optimality_proven: false`).
- Resolving a large exact tie on market-spread distance runs one Python call per tied candidate
  (only degenerate inputs produce such ties).
- An exact block always holds the whole deposit-side combination row, so a very large deposit
  side bounds memory by that row, not by the block size. This is capped by the exact threshold.
- The segmented path still bypasses the ML champion registry (pre-existing; lineage is reported
  as synthetic).
- The segmented and joint boundary-expansion rules differ (pre-existing), so single-tier
  equivalence is guaranteed only without expansion.
- A relaxed-ROE warning lives in `BoundaryDiagnostics` and can set `expansion_capped`
  (pre-existing).
