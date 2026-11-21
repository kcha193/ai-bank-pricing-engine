# Phase 3b verification: multi-period cohort cash flows (FIN-002)

Date: 2026-11-21. Branch: `fix/phase-3b-multiperiod-economics` (from master `5141833`, PR #78
merged). Finding: FIN-002 (P1) (`2026-11-13-remediation-roadmap.md`, row 3b). Offline only:
synthetic data, no live fetching, no external APIs, no production writes.

## Root cause

`scenarios/multiperiod.py` carried the earlier book as two running numbers: loan *volume* and
deposit *interest cost*. In each later period it added

```text
surviving_loan_volume x (cash_rate + reinvestment_spread_bps) - surviving_deposit_cost
```

to the period's new-business objective. So the surviving book:

| Term | Old treatment |
| --- | --- |
| Loan interest | Repriced at cash rate + reinvestment spread, not the booked loan rate |
| ECL | Omitted |
| Capital charge | Omitted |
| Wholesale funding | Omitted |
| Shortfall penalty | Omitted |
| Excess-deposit income | Omitted |
| Deposit interest | Kept at the booked rate |

Funding terms were also never computed on the combined book. Reference rates did not drift with
competitor rates, so a `pricing_strategy` spread grid stayed centred on the period-0 benchmark.

## Design

- `scenarios/cohort_accounting.py` (new, pure): `BookedCohort` (origination period, loan and
  deposit rate, original balances, booked ECL, effective RWA weight, hurdle; income, deposit cost
  and capital are derived, not stored); `BookedCohort.from_result` rejects a result whose income,
  deposit cost or capital does not reconcile to its rates; `survival_factor`, `position_at`,
  `ActiveCohort`; `evaluate_active_portfolio` (cohort terms summed, funding once on aggregates with
  `expected_wholesale_funding_cost`, `expected_excess_deposit_income`, `calculate_funding_result`).
- `scenarios/multiperiod.py`: revalidates the config; books each period's winner; checks the new
  cohort alone reproduces the optimiser's objective; evaluates survivors plus the new cohort;
  reports `PeriodEconomics`, `booked_cohorts`, `provenance`, `cohort_summary()`; extends
  `period_summary()` after the five legacy columns; warns that `reinvestment_spread_bps` is not
  applied; drifts reference rates with competitor drift (`reference_rate_drift`), rejects negative
  drifted rates.
- `config.py`: `MultiPeriodConfig.reference_rate_drift: "track_competitor" | "hold"` (default
  `track_competitor`).

Equations, conventions and limitations: `docs/modelling_assumptions.md` →
"Multi-period cohort accounting".

Unchanged: single-period objective and optimisers (joint and Phase 3a segmented), elasticities,
ECL, funding-spread and capital calibration, customer generation and seed, rate extraction, data
quality, API, LLM governance, champion approval, config hashing.

## Original audit reproduction

`2026-11-13-evidence/fin/challenge_core.py::multiperiod_case` (unchanged script): base config,
two periods, zero runoff, zero discount, no drift.

| | Before (audit / master `5141833`) | After |
| --- | --- | --- |
| A. Period-0 objective | $5,920,859.37 | $5,920,859.37 |
| B. Period-1 new-business objective | $5,920,859.37 | $5,920,859.37 |
| C. Period-1 surviving-book contribution | -$570,791.92 | +$5,920,859.37 |
| D. Period-1 full portfolio contribution | $5,350,067.45 | $11,841,718.74 (= 2 x A) |
| E. Two-period NPV | $11,270,926.82 | $17,762,578.11 (= 3 x A) |

The audit's own "consistent back book" figure was $5,920,859.37; the implied back book now
equals it exactly.

F. Period-1 active-book breakdown (after): loan interest $63,136,971.42; ECL $6,876,327.81;
capital $4,283,930.21; deposit interest $26,641,168.42; wholesale funding $13,400,539.96;
shortfall penalty $93,286.29 (shortfall $5,182,571.46); excess-deposit income $0. Each is twice
period 0 within 1e-9 relative (`test_t18_t19_t44_fin002_zero_runoff_back_book_matches_period_zero`).

The audit's drift case (100 bp/period on cash and both competitor rates, three periods) selects
the same rates and objectives before and after (6.19/4.05, 6.64/4.45, 7.19/4.85). Period 2 now
needs one expansion round instead of two, because its spread grid is centred on the drifted
reference rate.

## Stress cases

`phase_3b_comparison.py` on `configs/base.yaml`, before = master `5141833`
(`phase_3b_comparison_before_master_5141833.json`), after = this branch
(`phase_3b_comparison_after.json`). "Back book" is the undiscounted period contribution minus the
new-business objective, computed the same way on both versions.

| Case | Periods | Back book by period, before ($m) | After ($m) | NPV before ($m) | NPV after ($m) |
| --- | --- | --- | --- | --- | --- |
| 1 Zero runoff (FIN-002) | 2 | -0.57 | +5.92 | +11.27 | +17.76 |
| 2 Full runoff | 2 | +0.00 | +0.00 | +11.84 | +11.84 |
| 3 Loan runoff 0.5 > deposit 0.1 | 3 | -5.61, -13.22 | +2.57, +3.42 | -1.07 | +23.75 |
| 4 Deposit runoff 0.5 > loan 0.1 | 3 | +4.81, +11.81 | +3.42, +5.55 | +34.39 | +26.73 |
| 5 Rising rates (+50 bp/period) | 3 | +4.05, +10.55 | +2.23, +1.87 | +30.79 | +20.29 |
| 6 Falling rates (-50 bp/period) | 3 | +0.48, -0.66 | +4.15, +8.25 | +19.74 | +32.32 |
| 7 Funding shortfall (deposits run off) | 3 | +12.75, +25.50 | +1.15, +2.30 | +56.01 | +21.22 |
| 8 Excess deposits (loans run off) | 4 | -13.32, -26.64, -39.96 | -2.88, -7.98, -13.07 | -56.24 | -0.25 |
| 9 Four vintages (0.3 / 0.5) | 4 | +2.26, +5.18, +7.89 | +3.19, +4.95, +5.94 | +39.02 | +37.76 |
| 10 Discount 8% (0.3 / 0.5) | 3 | +2.26, +5.18 | +3.19, +4.95 | +21.31 | +21.92 |

Readings:

- Case 3 was the audit's failure mode: the old model let deposits outlive their loans and then
  charged their cost against a loan book repriced at the cash rate.
- Case 7: the old model charged no funding on surviving loans whose deposits had run off. After:
  period-2 loans $1,530m against deposits $329m give wholesale funding $44.4m and a shortfall
  penalty $12.0m (shortfall $665.6m).
- Case 8: surviving deposits now earn the cash rate on the surplus (period 3: $806m x 2.5% =
  $20.1m), but cost their booked 4.05%; the negative carry is real under these assumptions.
- Cases 5 and 6: booked cohorts keep their period-0 rates; only new business and the funding terms
  move with the market.
- No case has a hard-coded target NPV; tests assert hand-calculated reconciliations instead.

## Reconciliation evidence

- Single cohort: `evaluate_active_portfolio([new cohort])` equals the optimiser's
  `total_objective` within 1e-9 relative / $1e-6 absolute for every period of every run (enforced
  at run time; a mismatch raises).
- Booking: income = rate x balance, deposit cost = rate x balance and capital =
  `expected_capital_charge` within the same tolerance (enforced at run time).
- Hand calculations (`tests/unit/test_multiperiod.py`): three-period roll 11.8 / 18.65 / 20.93125
  with discount factors 1/1.1, 1/1.21, 1/1.331; mixed runoff 6.6; cash drift 19.6 / 9.8.
- Portfolio funding (`tests/unit/test_cohort_accounting.py`): shortfall on totals (75, not the
  cohort sum 225); excess income once (32, not 40 - 10 = 30).
- Attribution: `active_portfolio_objective = new_business_objective +
  back_book_incremental_contribution` and `discounted_contribution = active x discount_factor`
  for every row; NPV = sum of discounted contributions.

## Tests (T01-T50)

| IDs | Where |
| --- | --- |
| T01-T07, T09-T17, T21-T26 | `test_cohort_accounting.py` (pure evaluator, hand-calculated values) |
| T08, T18-T23, T27-T44 | `test_multiperiod.py` (fake periods with an independent calculator, plus real-engine runs) |
| T45-T50 | Full suite: Phase 3a segmented/joint golden tests, config validation, API, dashboard, Phase 1/2 regressions |

Changed expectations: the old tests that asserted the reinvestment-rate back book
(`test_back_book_loan_reinvestment_uses_period_cash_rate`,
`test_reinvestment_spread_raises_back_book_income`, `test_back_book_deposit_cost_carries_booked_rates`,
`test_discounting_and_back_book_runoff_are_applied_by_period`,
`test_tenor_derived_runoff_is_capped_at_one`) encoded the FIN-002 economics and were replaced by
hand-calculated cohort tests. Their fakes reported a `total_objective` unrelated to their
components, which the new run-time reconciliation correctly rejects.

## Verification

- `python -m ruff check .`: all checks passed.
- `python -m pytest --cov=balance_pricing_engine --cov-report=term-missing --cov-fail-under=95
  -m "not live_rates" -q`: 3581 passed, 0 failed, 2 skipped; total coverage 97.05%;
  `scenarios/cohort_accounting.py` 100%, `scenarios/multiperiod.py` 100%.
- Focused: `test_multiperiod.py` + `test_cohort_accounting.py` 67 passed, including a segmented
  (two loan tiers) roll that reconciles to the Phase 3a solver's objective.
- `examples/run_multiperiod.py` runs and prints the decomposition and cohort table.
- An independent review pass found no formula, timing, discounting or double-counting defect;
  its test-quality points (a misnamed T39 case, a redundant assertion) and a docstring wording
  point were fixed.
- Config hashing is unchanged as a method. A config with a `multiperiod` block now hashes the
  added `reference_rate_drift` default; configs without one (including `configs/base.yaml`) are
  unaffected.

## Remaining assumptions and limitations

- Geometric runoff and the `1 / loan_tenor_years` approximation; no amortisation schedule,
  prepayment, maturity or rollover.
- Constant booked loss rate on survivors; not lifetime or IFRS 9 ECL.
- Wholesale funding and surplus cash reprice every period at the period market.
- No terminal value; balances outstanding after the last period are not valued.
- Each period's new business is an expected draw from the same seeded prospect population.
- Myopic new-business pricing; not a multi-horizon optimisation.
- Synthetic response only (no model registry in the roll).
