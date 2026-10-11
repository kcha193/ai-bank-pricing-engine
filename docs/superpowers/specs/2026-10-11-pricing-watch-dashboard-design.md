# Pricing Watch Dashboard — Design Spec

Status: draft for review · Date: 2026-10-11 · Branch: `feature/pricing-watch-dashboard`
Path: architectural (new UI subsystem + one new backend read-model seam)

## 1. Purpose and success criteria

The NZ Pricing Intelligence backend (collect → validate → persist → detect → snapshot →
market state → health → scenarios) is verified but operated through the CLI. This work makes a
Streamlit dashboard the primary human operating console. It must answer, fast:

1. What is happening in NZ mortgage pricing now?
2. What changed recently?
3. Where does each bank sit competitively?
4. What does the pricing model say about possible responses?
5. Is Pricing Watch itself healthy and current?

Success = the acceptance list in the task brief, plus these design invariants:

- **Thin UI.** The dashboard formats; it never computes rankings, bps movements, freshness,
  market state, or scenario economics.
- **Reads never collect.** No code path reachable from a Streamlit rerender, `Refresh
  Dashboard`, or page navigation can start a live collection.
- **Decision support only.** Nothing in the dashboard changes a real bank price or feeds an
  automated pricing decision.
- **Trust over coverage.** Where the backend cannot support a spec item, the page says so
  instead of inventing a value (§3).

Assumptions (not stated by the user, chosen by me): mortgage (`HOME_LOAN`) is the market view
throughout; System Health additionally keeps the term-deposit collector/run tables that the
current dashboard already shows (no regression). Operational timestamps render in
`Pacific/Auckland` via stdlib `zoneinfo`.

## 2. What exists today

- `src/balance_pricing_engine/dashboard/app.py` — single Streamlit script (market section +
  YAML-driven profit-surface/sensitivity run), executes at import; tested whole-script via
  `AppTest` in `tests/unit/test_dashboard.py`, `test_dashboard_runtime.py`,
  `test_dashboard_market_intelligence.py`.
- `pricing_intelligence/dashboard_data.py` — Streamlit-free pandas shaping; includes
  `build_freshness_frame` and `build_collector_health_frame`, each applying a staleness
  threshold. `cli._health` applies a third copy; `service._exclude_stale` is the fourth
  (record-level, drives the snapshot). **Freshness is duplicated, which this feature must not
  extend.**
- `application/PricingService` wraps the optimiser with governance/lineage — for
  portfolio-level runs, not market scenarios. Market scenarios already go through
  `pricing_scenario_trigger.run_triggered_scenarios` → `strategy.apply_market_strategy_to_config`
  → `run_baseline_diagnostics`. The dashboard reuses that seam.
- Operational entry points: `scripts/collect_market_rates_workflow.py` (lock, exit codes
  0/1/2/3, `--summary-file` JSON, scenario trigger wiring, no dry-run) and CLI
  `pricing_intelligence run --dry-run` (read-only store, console alerts, text output only).
  `backup.backup_database`, `alerts.build_telegram_sink_from_env`.
- Repo gates: CI = `ruff check` + `pytest`. `ruff format --check .` would reformat 110
  existing files; mypy is installed but unconfigured. New files are held to `ruff format` and
  `mypy`; existing files are not reformatted.

## 3. Spec items the backend cannot support as written

| Brief item | Backend reality | Decision |
|---|---|---|
| Scheduler `ACTIVE` | No runtime scheduler signal exists | Show "Last run: N h ago (status)". No ACTIVE/INACTIVE. |
| Event previous/new rank, market median at event time | Not stored on `RateChange` | Derive from `get_market_snapshot(as_of_run_id=…)` for the event's run and the run before it. If the earlier run cannot be resolved cleanly, show "n/a" (plan-stage verification point). |
| Effective date / parser version on an event | On `CompetitorRateRecord`, not `RateChange` | Look up the observation via the store (history by bank/term/category); show when found. |
| "Westpac excluded: 51 h old" | Snapshot silently drops stale records | New read-only accessor reusing the same predicate (§5.2). Message shows the **configured** threshold (default 168 h), not an illustrative one. |
| Scenario row "Current 4.89%" | Backend never reads the bank's real price. "No change" = optimiser recommendation under an unchanged competitor assumption | "Current rate" is an operator-entered input used only for `get_competitive_position`. "No change" is labelled "No-change optimum", matching `format_scenario_block`. |
| `MODEL_OPTIMUM` / `NO CHANGE` strategies | Neither is a `CompetitorRateStrategy`. "Model optimum" is the `MARKET_MEDIAN` row relabelled | Strategy selector exposes only `MANUAL, MARKET_MEDIAN, MARKET_LEADER, MARKET_SECOND_LOWEST, MARKET_MEAN` (+ offset bps). Comparison table = the four `run_triggered_scenarios` rows + "Model optimum". |
| "Volume / Margin" columns | `ScenarioRow` carries `competitor_rate, our_rate, objective` only | Show those. Volume/margin appear only in the single-scenario detail drawn from `OptimizationResult` of the selected strategy. |

## 4. Architecture

```
pages/*.py      render only (st.*, altair)         ┐ dashboard/ package
charts.py       altair specs from view-model data   │ (may import both layers;
formatting.py   pure unit/format helpers            │  boundary tests only restrict
services.py     DashboardServices → view models     │  optimiser→intelligence and
actions.py      operational actions (subprocess)    ┘  intelligence→optimiser)
        │
pricing_intelligence/health.py   NEW  one source for freshness/health/exclusions
pricing_intelligence/service.py  existing facade (snapshot, changes, history, state, position)
pricing_scenario_trigger.py      existing scenario seam
backup.py, alerts.py             existing
```

Dependency rules (enforced by tests): `services.py`, `formatting.py`, `charts.py`,
`actions.py` import no `streamlit`; `pricing_intelligence/` imports no optimiser (existing
boundary test stays green); `dashboard/pages` import only `services`, `formatting`, `charts`.

`app.py` becomes a thin router (`st.set_page_config`, `st.navigation`, shared header). The
launch command is unchanged: `streamlit run src/balance_pricing_engine/dashboard/app.py`.

### 5. Backend additions (additive, behaviour-preserving)

**5.1 `pricing_intelligence/health.py`.** Single home for freshness semantics.

- `FreshnessState` enum: `FRESH | STALE | UNKNOWN`.
- `assess_bank_health(...)` → `BankHealth`: bank, `last_success_at`, `observation_age_hours`
  (oldest confirmed current observation, as `build_freshness_frame` defines it),
  `consecutive_failures`, `latest_record_count`, `freshness: FreshnessState`,
  `health: HEALTHY | DEGRADED | FAILING | UNKNOWN`, `quarantined: bool`, `last_error`.
  Rules are lifted verbatim from `cli._health` / `build_collector_health_frame` /
  `build_freshness_frame`, using `config.max_observation_age_hours`; no new thresholds.
- `assess_system_health(service, product)` → `SystemHealth`: database integrity
  (`check_integrity`), latest run record, per-bank `BankHealth`, Telegram/webhook configured
  booleans, snapshot validity.
- `market_exclusions(service, product, now)` → `tuple[Exclusion]` with bank, reason
  (`STALE | QUARANTINED | NOT_COLLECTED`), age, threshold. Uses the same
  `_exclude_stale`/suspicious predicates the snapshot uses (extracted to a shared helper) so
  explanation and exclusion cannot diverge.
- `cli._health` and `dashboard_data.build_collector_health_frame`/`build_freshness_frame`
  delegate to this module. Existing CLI/dashboard-data tests are the regression net; behaviour
  and output text are unchanged.

**5.2 Event context.** `events_with_context(service, changes)` (in `health.py` or a sibling
`event_context.py`, decided in the plan) returns, per change: previous/new rank, market
median at the event run, market state, and the matching observation's `effective_date_text`,
`parser_version`, `source_url`, `rate_category`. Pure reads over existing store/service
methods; `None` fields when not derivable.

No other `pricing_intelligence` changes. `RateChange`, store schema, and the regression
artifact contract (`summary.json` etc.) are untouched.

## 6. Services layer (`dashboard/services.py`)

`DashboardServices(config, *, clock=…)` opens a read-only `ObservationStore` per call (WAL
reads; closed after use) and returns frozen view-model dataclasses. Every method returns
`Result[T]` = `OK(value) | EMPTY(reason) | ERROR(message)`; exceptions are logged with detail
and mapped to a user-safe message (e.g. `sqlite3.Error` → "Market database unavailable").

| Method | Backend source |
|---|---|
| `overview()` | `get_market_snapshot`, `get_market_state`, `get_changes(today / 7d)`, `assess_system_health` |
| `term_market(term)` | `TermMarket` (leaders, lowest/median/highest, spread, `ranking()`) — tied ranks use backend standard-competition semantics |
| `bank_positions(term, bank?)` | `TermMarket.rank_for`, `get_competitive_position`, last change from `get_changes` |
| `changes(window, filters)` | `get_changes(days/since, bank, term, scope_to_market=True)` + `events_with_context` |
| `history(banks, term, range, category)` | `get_rate_history_series`; median/leader/spread series from per-date snapshot values via the same `TermMarket` math (no re-implementation: series built by constructing `TermMarket` per date) |
| `scenarios(term, strategy, manual_rate, offset_bps, current_rate)` | `resolve_competitor_assumption`, `run_triggered_scenarios`, `run_baseline_diagnostics` (objective curve from `grid_results`), `get_competitive_position` |
| `health()`, `runs()`, `run_detail(id)`, `suspicious()` | `assess_system_health`, `store.get_recent_run_records`/`get_run_record`, suspicious changes + quarantined observations |

History date-series note: market median/leader/spread per date must reuse `TermMarket`
(built from the representative rates `get_rate_history_series` already resolves) rather than
`statistics.median` in the dashboard. Whether `build_market_snapshot` can be called per date
or `TermMarket` constructed directly is a plan-stage detail; either way the arithmetic is the
backend's.

Scenario inputs are passed through unmodified and asserted by test: term →
`term_months`, strategy → `strategy`, manual rate → `manual_rate` (percent → fraction
conversion happens in exactly one named function), offset → `offset_bps`. Scenario errors
(`MarketStrategyError`, "no peer once our_bank excluded", `NotImplementedError`) become an
ERROR result with the backend message shown as an operational note.

## 7. Pages

All pages share a header: **NZ Retail Bank Pricing Watch**, last successful update
(Pacific/Auckland), market activity (backend `MarketState`), collector health `N/M fresh`.
Freshness/stale callouts render from `BankHealth`/`Exclusion`, never recomputed.

1. **Overview** — executive metrics (changes today / 7d, banks repricing, dominant direction,
   most active term — all from `MarketStateAssessment`/`get_changes`); bank × term matrix
   (rates `4.85%`; leader marked with ◆ and bold, stale marked ⚠ with text "stale", missing
   shown "—", special/standard shown by the backend `rate_category` label, never by colour
   alone); term selector → leader, lowest, median, highest, spread, banks observed, fresh
   banks, ranking with ties as `2. BNZ / 2. Westpac / 4. ASB`.
2. **Market Watch** — filters (term, bank, rate type); per-bank card/table: rate, rank,
   vs leader (bps), vs median (bps), last change, freshness. Competitive-position dot plot
   with leader/median/selected-bank markers.
3. **Rate Changes** — window Today / 7d (default) / 30d / Custom; table (timestamp, bank,
   term, old, new, movement, prev/new rank, type); filters bank/term/direction/type; event
   detail panel (source link, observed time, effective date, rate type, ranks, median, market
   state, parser version). Raw HTML never shown. Suspicious events are excluded here and shown
   in System Health.
4. **Historical Trends** — multi-select banks, term, range, rate type; one multi-series line
   chart; median/leader/spread toggles; factual repricing timeline (bank, bps, offset in days
   from first mover; no causal language).
5. **Pricing Scenarios** — inputs per §6; comparison table (§3); one objective-curve chart
   (our rate × objective) annotated at current, market median, leader, model optimum when
   present; deterministic decision-support text templated from backend numbers; permanent
   "Decision support only — this dashboard never changes a bank price" banner. "Scenario Lab"
   tab keeps the existing YAML profit-surface/sensitivity run unchanged in behaviour.
6. **Operations** — described in §8.
7. **System Health** — component table (Database, Market snapshot, Last run, Telegram,
   Webhook; scheduler row replaced by last-run age), collector table (last success, age,
   failures, latest record count, freshness, health), run history with drill-down
   (`run_detail`), suspicious/quarantined observations in a visually separate section
   ("Excluded from trusted benchmark"), term-deposit collector/run tables retained.

## 8. Operations and safety

Controls are visually and semantically distinct:

- **Refresh Dashboard** — clears read caches and reruns. Imports nothing from `actions.py`.
- **Run Pricing Watch** (live) — requires an explicit confirm checkbox + button. Spawns
  `sys.executable scripts/collect_market_rates_workflow.py --config <cfg> --database <db>
  --render-javascript --summary-file <tmp>` as a subprocess (same lock, exit codes, scenario
  trigger as scheduled runs). Exit 3 → "Already running", 1 → failed, 2 → partial, 0 → ok.
  Result parsed from the summary JSON (`build_summary` keys: `banks_succeeded`,
  `banks_failed`, `rates_accepted`, `changes_detected`, `suspicious_changes`, `failures`) into a
  per-bank table + elapsed time. The subprocess keeps Playwright out of Streamlit's script
  thread and survives a closed tab.
- **Dry Run** — subprocess of a small worker `python -m
  balance_pricing_engine.dashboard.run_worker --dry-run` wrapping
  `run_operational_cycle(dry_run=True)` on a `read_only` store, writing a JSON result. UI
  states exactly: collects live pages; persists nothing; sends no alerts; does not update
  collector health. (If a spike shows Playwright is safe in-process, the plan may simplify;
  default is subprocess.)
- **Create Backup** — `backup_database(db, destination, keep=…)`; shows timestamp and
  repo-relative path. Default destination is a git-ignored directory under `data/`.
- **Send Test Notification** — `build_telegram_sink_from_env()`; if `None` shows "Telegram is
  not configured" (no variable names' values ever shown); sends `plain_alert(...)` as
  `_telegram_test` does; errors map to "Telegram test failed" with detail in logs.

State machine (session state): `idle → running(action) → done|failed`. Actions fire only
from `on_click` callbacks; an in-flight flag disables all action buttons and prevents a
double-submit; a rerender during `running` re-reads the flag and renders status, never
re-invokes the action. `actions.py` has a single `launch(...)` entry point per action so tests
can patch it and count calls.

No action touches pricing: nothing in the dashboard writes a rate to any pricing system.

## 9. State and caching

- Reads: `st.cache_data(ttl=60)` keyed on `(db_path, db_mtime_ns, args)`. Run completion and
  Refresh Dashboard clear it.
- **Never cached:** `assess_system_health`, `market_exclusions`, anything with `now`.
- Session state holds only: selected term/bank filters, action state machine, last action
  result. Widgets use stable keys; filter defaults are set once.

## 10. Errors, freshness, empty states

- User-visible errors are one-line operational messages with a pointer ("See System Health").
  Stack traces go to the logger only.
- Failed collector: the last validated rate stays visible but marked STALE/⚠ with the
  backend's exclusion reason; matrix never shows a stale rate as current.
- Empty states: no DB (how to collect), empty snapshot, no changes in window, no history,
  Telegram unconfigured, scenario error — each a specific sentence; never raw `[]`/`None`/`NaN`.
- Suspicious observations never share a table with trusted rates.

## 11. Charts (Altair; no new dependency)

Dot plot (rate vs bank, leader/median/selected markers), multi-bank history lines, median /
leader / spread trend, repricing timeline (dots on a time axis), scenario objective curve
with vertical rule annotations. Units: rates `%`, movements `bps`, money via existing
`reporting.fmt_money`. No pies/3D/gauges. Charts consume view-model data only.

## 12. Formatting (`formatting.py`)

Explicit-unit helpers so percent/fraction cannot be confused (snapshot rates are percent
`4.89`; scenario rates are fractions `0.0489`): `fmt_rate_pct`, `fmt_rate_fraction`,
`fmt_bps` (`-10 bps`, signed), `fmt_nz_datetime` (`11 Oct 2026 08:03`), `fmt_age_hours`,
`fmt_status`. Formatting never rounds underlying values; bps display uses the backend's
`Decimal` value.

## 13. Testing

TDD, behavioural, no pixel tests.

- `formatting`: rates, bps sign/zero, timestamps incl. DST, status labels.
- `health.py`: freshness states from fixtures (fresh/stale/unknown/quarantined/failing);
  regression: CLI `health` output and `build_collector_health_frame` unchanged.
- `services`: each method against fixture stores built with the existing
  `fixture_fetcher`/`seed` pattern; asserts values equal direct backend calls (matrix cells,
  leader, median, spread, ranking ties).
- Scenario pass-through: patch `run_triggered_scenarios`/`resolve_competitor_assumption`,
  assert term/bank/strategy/manual-rate/offset arrive exactly; displayed numbers equal the
  patched return.
- `actions`: patch `subprocess`/`backup_database`/telegram sink; assert exactly-once launch,
  exit-code mapping, unconfigured Telegram, backup result, no credentials in output.
- **Safety** (`AppTest`): render every page + click Refresh Dashboard + several reruns with
  `actions.launch` patched → `call_count == 0`; click Run once → `== 1`; rerender while
  running → still `== 1`.
- Failure states: missing DB, corrupt/locked DB (`sqlite3.Error`), empty DB, one collector
  failed (others continue; failed bank STALE/DEGRADED; stale not in matrix), stale-only
  store, scenario error.
- Architecture: import-boundary test (no streamlit in services/actions/formatting/charts;
  intelligence still doesn't import optimiser).
- Migrate the three existing dashboard test files to the router/pages; keep their intent.
- Full existing suite stays green (baseline: 1603 passed, 2 skipped).

## 14. Documentation

Update `README.md` and `docs/pricing_intelligence.md` (Dashboard section): launch command
(note `PYTHONPATH=src` when running from a worktree), each page, Refresh vs Run semantics,
freshness semantics (observation-age vs collector-success; configured threshold), scenario
semantics (what "No change"/"Model optimum" mean), decision-support limitation, operational
controls, troubleshooting. Update `docs/architecture.md` module map and `AGENTS.md`
(dashboard seam rules: no freshness/ranking/bps/economics in UI; reads never collect).

## 15. Task decomposition (feeds writing-plans)

T1 `health.py` + CLI/frame delegation (primary) → T2 services/formatting/Result seam
(primary) → T3 router + Overview → T4 Market Watch → T5 Rate Changes (+ event context)
→ T6 Historical Trends → T7 Pricing Scenarios → T8 Operations (actions + worker + safety
tests, primary) → T9 System Health → T10 docs + test migration + e2e verification.
Parallelisable by subagent after T2 merges: T5, T6, T9, docs (disjoint files).

## 16. Known limitations (stated up front)

No scheduler state; run progress is spinner-then-table (no per-bank streaming); event ranks
may be "n/a" for runs the store cannot reconstruct; volume/margin only in single-scenario
detail; repo-wide `ruff format` not applied; mypy applied to new files only.
