# Phase 2c verification: market freshness and minimum bank coverage (2026-11-16)

Findings: DATA-003 (P2) and DATA-004 (P2) from
[2026-11-13-full-repository-audit.md](2026-11-13-full-repository-audit.md).
Base: `master` @ `de970af` (PR #73 merged).

## Root causes

**DATA-003 (one-bank "market", hidden input age).**

1. `strategy.resolve_competitor_rate` accepted any non-empty `TermMarket`: a single bank
   produced "median advertised rate across 1 banks" (`strategy.py:144,173` at base).
2. `ResolvedCompetitorAssumption.as_of` is the snapshot's `as_of`, which
   `build_market_snapshot` sets to the **newest** input (`snapshot.py:306`). Nothing reported the
   oldest input, so four 145 h-old inputs and one fresh one read as "current".
3. No configuration expressed a minimum (`config.py:131`).

**DATA-004 ("Fresh" after a failed collection).**

1. A total failure short-circuits before `record_observations()`, so it writes no
   `collection_runs` row (`service.py:727-760`). Only `operational_runs` records the attempt.
2. `build_freshness_frame` and the dashboard's freshness counts were driven by observation age
   and `collection_runs` identity only (`dashboard_data.py:25`, `store.py:2296`). Rates still
   inside the 168 h window therefore read "Fresh" after the most recent attempt failed.

## Fixture reproductions (original scripts, unedited)

Evidence: `docs/audits/2026-11-16-phase-2c-evidence/`. `case_H_after.out` and `case_I_after.out`
are the unedited 2026-11-13 scripts re-run on this branch; `case_H_I_after.py`/`.out` replays the
same shapes through the new interfaces with the same harness.

### Case H (only ANZ reachable from day 1)

| Day | Before (`de970af`) | After |
|---|---|---|
| 1 | 4.850%, "across 5 banks", `as_of` = newest input | 4.850%, 5 eligible / min 3, `SUFFICIENT`, oldest input 25 h |
| 6 | 4.850%, "across 5 banks"; 4 inputs 145 h old, not reported | 4.850%, 5 eligible / min 3, **`SUFFICIENT_AGED`, oldest input 2026-11-10 18:30 (145 h)**; `as_of` still the newest |
| 8 | **4.790% "median across 1 banks"** (TD: 4.000%) | **Refused**: `Insufficient competitor coverage for 12-month home loans: 1 eligible bank; minimum required: 3.` (TD: same, "term deposits") |
| 30 | `MarketStrategyError` (all stale) | unchanged |

### Case I-b (seeded TD market, then every TD fetch fails)

| | Before | After |
|---|---|---|
| Exit code | 1 FAILED | 1 FAILED (unchanged) |
| Freshness frame `Status` (T0+1d) | `Fresh` × 5 | `Degraded — last collection failed; latest trusted observations are 25 hours old` × 5, attempt status `FAILED` |
| Fresh bank count | 5 | rate-age count 5 (unchanged meaning), **confirmed fresh 0** |
| Run summary | — | `Latest collection failed: 5 of 5 banks failed` |
| T0+8d | `Stale` | `Stale — latest trusted observations are 193 hours old; excluded from market statistics; last collection failed` |
| Observations | kept | kept, timestamps unchanged (audit and change detection intact) |
| Home-loan side | — | unaffected (`UNKNOWN`: no home-loan run recorded) |

The unedited `case_I_td_zero_rows.py` still calls `build_freshness_frame` without the new
`latest_run=` argument, so its frame keeps the legacy rate-age labels ("Fresh"). That legacy
call shape describes observation age only; no production caller uses it (the dashboard and CLI
read `assess_system_health`). See Compatibility.

## Policy

**Minimum bank coverage.** `PricingIntelligenceConfig.min_banks` (default 3, strict integer
≥ 1; an explicit value above `len(selected_banks)` is rejected) counts distinct eligible banks
per product and term, after staleness, quarantine fallbacks, rejected/voided readings,
non-comparable and name-excluded products, the Phase 2b special policy and the own-bank
exclusion. Enforced in `strategy.resolve_competitor_rate`, the seam used by the service, CLI,
`run_triggered_scenarios` (scheduled triggers and Pricing Studio), joint overlays and the example.
Below the minimum: `InsufficientCoverageError` (subclass of `MarketStrategyError`, so existing
422 mapping holds); never a fallback to the configured rate. `min_banks=None` = the default 3.
Descriptive views keep observed data visible with one warning. MANUAL is unaffected and
disclosed as an assumption.

**Input age.** `ResolvedCompetitorAssumption.coverage` (`BenchmarkCoverage`) reports eligible
banks, minimum, evaluation time, newest and oldest contributing observation, oldest age and a
status (`SUFFICIENT`, `SUFFICIENT_AGED` above `aged_input_hours` = 48 h, `INSUFFICIENT`). Ages use
`MarketSnapshot.evaluated_at` (`now` live; run start for `as_of_run_id`), never the wall clock
during reconstruction.

**Operational freshness.** `health.assess_operational_freshness` combines observation freshness
with the product's latest `operational_runs` attempt: QUARANTINED > STALE > UNKNOWN (no trusted
observation, or no ledger) > DEGRADED (bank failed / not attempted / not fully reconfirmed) >
FRESH. Products are independent.

## Tests

New: `test_pricing_intelligence_coverage.py` (T11–T18, T21, T23–T25, T28, T35),
`test_pricing_intelligence_operational_freshness.py` (T01–T10),
`test_pricing_intelligence_phase2c_audit_replay.py` (T19, T20, T25, T31, T32),
`test_dashboard_coverage_freshness.py` (T22, T23, T28–T30), trigger tests T26/T27 in
`test_pricing_scenario_trigger.py`, CLI refusal/evidence in `test_pricing_intelligence_cli.py`.
Existing thin-market fixtures that test other invariants now pass `min_banks` explicitly.

## Compatibility

- `resolve_competitor_rate`/`apply_market_strategy_to_config` default to a three-bank minimum:
  a direct caller with a one- or two-bank snapshot now gets `InsufficientCoverageError` and must
  pass `min_banks` deliberately. New keyword-only `min_banks`, `now`, `aged_input_hours`.
- Additive fields only: `ResolvedCompetitorAssumption.coverage`, `MarketSnapshot.evaluated_at`,
  `BankHealth.operational`, `HeaderInfo.confirmed_fresh_banks/collection_status/collection_summary`,
  `TermSummary.coverage`, `ScenarioView.loan_coverage/deposit_coverage/coverage_warning`,
  `ScenarioComparison.coverage`. `SystemHealth.fresh_bank_count` keeps its rate-age meaning.
- `build_freshness_frame` keeps its three legacy columns unless `latest_run=` is passed.
- Dashboard "fresh" figures now count confirmed-fresh banks; a database with no
  `operational_runs` rows shows them as 0 (UNKNOWN) rather than inferring success.

## Remaining risks

- The CLI `collect` command writes observations without an `operational_runs` row; after it, the
  operational state reflects the last ledgered attempt (scheduled workflow and dashboard runs
  are ledgered).
- Historical `as_of_run_id` snapshots remain exempt from the staleness filter (existing
  behaviour); coverage still reports their oldest input age against the run time.
- Collector health stamps wall-clock time, so "Last success" can differ from the observation
  capture time in tests using a business clock (identical in production).
- `min_banks` = 3 suits the five tracked banks; a smaller tracked set must lower it explicitly.
