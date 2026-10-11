# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Pricing Watch dashboard** (`src/balance_pricing_engine/dashboard/`): a seven-page
  Streamlit operating console — Overview, Market Watch, Rate Changes, Historical Trends,
  Pricing Scenarios (with the former YAML runner as *Scenario Lab*), Operations and System
  Health — over the existing pricing-intelligence services. Refresh Dashboard reads stored
  state only; Run Pricing Watch / Dry Run start a live collection only from an explicit click.
  Decision support only. See [docs/pricing_intelligence.md](docs/pricing_intelligence.md#dashboard).
- `pricing_intelligence/health.py`: one implementation of freshness, collector status and
  system health, plus `PricingIntelligenceService.get_market_exclusions()` (why a record was
  left out of the market snapshot), `snapshot.term_market_from_rates` and
  `strategy.apply_manual_competitor_rate_to_config`.
- **`Pricing Watch.command`**: a macOS double-click launcher in the repo root. Runs the
  dashboard from the repository it lives in, on `127.0.0.1` only, loads `./.env`, reuses a
  running instance, and opens the browser when the server is ready. `.env` and `.env.*`
  (except `.env.example`) are now git-ignored, as `.env.example` already claimed.
- Operations safeguards: optional `PRICING_WATCH_OPS_TOKEN` gate, single-flight runner with a
  cooldown, backup and Telegram rate limits, process-wide unlock attempt limiter.
- Environment variables `PRICING_WATCH_CONFIG` and `PRICING_WATCH_OPS_TOKEN` (see `.env.example`).

### Changed

- The `pricing_intelligence health` command and the dashboard frames now delegate to
  `health.py`; their output is unchanged.
- The single-script dashboard was replaced by the multi-page console. Launch command unchanged:
  `streamlit run src/balance_pricing_engine/dashboard/app.py`.

## [0.2.0] - 2026-09-09

**NZ Pricing Intelligence — Operational Monitoring.** Turns the verified
mortgage pricing-intelligence capability (v0.1.0's `pricing_intelligence/`
layer) into a daily operating workflow: scheduling, Telegram alerts,
collector health monitoring, daily/weekly briefs, and auto-triggered pricing
scenarios. See [docs/pricing_intelligence.md](docs/pricing_intelligence.md)'s
"Pricing Intelligence Operations" section for full usage.

### Added

- **Telegram alert delivery** (`TelegramAlertSink`, `pricing_intelligence
  telegram-test`), alongside the existing generic webhook — both can be
  configured at once. A per-channel `notified_alerts` ledger guards delivery
  idempotency for a retried run.
- **Unified operational run command**: `pricing_intelligence run`
  (interactive) and the scheduled `scripts/collect_market_rates_workflow.py`
  both now call one shared `orchestration.run_operational_cycle()` —
  collect → validate → persist → detect changes → classify market state →
  update collector health → dispatch alerts → persist a `RunRecord`, in one
  call. `--dry-run` previews the whole cycle with no persistence and no real
  alerts.
- **Persisted operational run ledger** (`RunRecord` / `operational_runs`
  table): status (`SUCCESS`/`PARTIAL_SUCCESS`/`FAILED`), per-bank
  attempted/successful/failed, rates observed/changed, validation failures,
  suspicious observations, market state, and an error summary — queryable
  historically, unlike the prior single-file JSON summary.
- **Collector health monitoring**: per-bank consecutive-failure streaks
  (`collector_health` table), configurable escalation (`HealthThresholds`:
  warning at 2 consecutive failures, alert at 3, both configurable), and
  automatic recovery notifications.
- **Daily and weekly intelligence briefs** (`pricing_intelligence brief
  [--weekly] [--send]`) — concise summaries of today's changes plus 7-day
  market activity, or a weekly repricing/collector-uptime summary.
- **Auto-triggered pricing scenarios**: a meaningful (non-suspicious) rate
  move on a tracked term automatically runs the No-change/Match-median/
  Match-leader/Leader-minus-5bps/Model-optimum comparison and appends a
  compact decision-support block to the delivered alert. Persisted to a new
  `scenario_runs` table. Decision support only — never changes a real price.
  Available through the scheduled workflow (`--config`); the interactive
  `pricing_intelligence` CLI does not support it, by the same architectural
  rule that forbids the intelligence layer from importing the optimiser.
- **`pricing_intelligence health`** — database integrity check, latest run
  status, per-bank collector freshness, alert-channel configuration, and
  market-snapshot validity in one command.
- **`pricing_intelligence backup`** — timestamped, transactionally-consistent
  SQLite backups with count-based pruning.
- Dashboard: Collector Health, Recent Runs, and Pricing Scenarios sections
  alongside the existing market-intelligence view.
- `.env.example` documenting every optional environment variable across the
  project (pricing intelligence, LLM layer, API auth).

### Changed

- `Alert.change` is now `Optional[RateChange]` (was required) — enables
  collector-health/recovery notices and briefs to reuse the existing
  `AlertSink`/`dispatch_alerts_safely` delivery machinery instead of a
  parallel path.
- `PricingIntelligenceService.collect()` accepts an optional `run_id` so a
  caller (the new orchestration layer) can correlate its own operational
  run id with the collection it produces.
- `build_summary()`/`exit_code_for()` (and their private bank-scoped-failure
  helpers) moved from `scripts/collect_market_rates_workflow.py` into
  `pricing_intelligence/orchestration.py` — core run-classification logic,
  not scheduler-specific; the script re-imports the same names.
- `scripts/collect_market_rates_workflow.py`'s single-instance lock moved to
  `pricing_intelligence/locking.py`, shared with the interactive `run`
  command.
- `deploy/scheduling/` examples document Telegram configuration alongside
  the existing webhook guidance; the crontab example now pins
  `TZ=Pacific/Auckland` explicitly rather than relying on the host's system
  timezone (launchd's `StartCalendarInterval` has no equivalent per-job
  timezone key — documented as a known limitation there).

### Fixed

- A backup-pruning slicing bug (`backups[:excess]` with a negative `excess`)
  that would have deleted almost all backups whenever fewer existed than the
  configured retention count — caught by the operational test suite before
  release, not shipped.

[0.2.0]: https://github.com/kcha193/ai-bank-pricing-engine/releases/tag/v0.2.0
