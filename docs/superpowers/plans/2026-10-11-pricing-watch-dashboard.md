# Pricing Watch Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing single-script Streamlit dashboard into a seven-page Pricing Watch operating console that reads stored state, explains freshness, supports scenarios, and safely operates collection — without recomputing any pricing intelligence in the UI.

**Architecture:** A new backend read-model (`pricing_intelligence/health.py`) becomes the single owner of freshness/health/exclusion semantics. A Streamlit-free `dashboard/services.py` returns frozen view models wrapped in a `Result`; pages only render. Live collection runs as a subprocess of the scheduled workflow script behind a single-flight `JobRunner`; reads never reach `actions.py`.

**Tech Stack:** Python 3.9+ (repo target), Streamlit 1.57 (`st.navigation`, `AppTest`), Altair (ships with Streamlit), pandas, pydantic v2, pytest, ruff.

**Spec:** [docs/superpowers/specs/2026-10-11-pricing-watch-dashboard-design.md](../specs/2026-10-11-pricing-watch-dashboard-design.md)

## Global Constraints

- Python `>=3.9`; ruff `line-length=100`, `target-version="py39"` — use `from __future__ import annotations`, `Optional[...]`/`typing` generics where runtime evaluation matters (pydantic/dataclass fields are fine with the future import).
- Import as `balance_pricing_engine.*`, never `src.balance_pricing_engine.*`.
- `pricing_intelligence/` must not import the optimiser (`optimization`, `grid_search`, `scenarios.runner`); `optimization/`, `pricing/`, `scenarios/`, `synthetic/` must not mention `pricing_intelligence`. Only `strategy.py` may contain `config.market.model_copy`.
- Dashboard `services.py`, `actions.py`, `formatting.py`, `charts.py`, `models.py`, `results.py`, `settings.py`, `scenario_text.py` import no `streamlit`.
- The dashboard never computes rankings, bps movements, freshness, market state or scenario economics; it calls backend functions. Rates: snapshot/`RateChange` are **percent** (`4.89`), scenario rows/`OptimizationResult` are **fractions** (`0.0489`) — convert only via `formatting.fmt_rate_pct` / `fmt_rate_fraction`.
- A rerender, page navigation or `Refresh Dashboard` must never start a collection. `Refresh Dashboard` imports nothing from `actions.py`.
- Operational timestamps render in `Pacific/Auckland` (`zoneinfo`), format `11 Oct 2026 08:03`.
- Telegram credentials are never shown, logged, or included in an error message; user-visible errors are one operational sentence, stack traces go to the logger only.
- Do not run `ruff format` over existing files (110 would change). New files must pass `ruff format --check` and `mypy` (run on new files only). CI gates: `python3 -m ruff check .` and `python3 -m pytest -q -m 'not live_rates'`.
- Run Streamlit from a worktree with `PYTHONPATH=src` (the editable install points at the main checkout).
- Baseline before this work: `1603 passed, 2 skipped`. Existing `GRID_RESULT_COLUMNS`/`summary.json` regression contract and `MANUAL`-returns-same-config-object invariants are untouched.
- Commits: `feat:`/`test:`/`refactor:`/`docs:` prefixes; end every commit message with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` (pass as a second `-m`).

## Review Focus

Inputs/conditions the spec implies but the happy-path tasks do not exercise; each has a pinning test in the named task.

1. **DB path missing → must not be created.** `ObservationStore(path)` creates an empty file for a nonexistent path; a read must return an EMPTY result and leave no file. (Task 2)
2. **Corrupt/locked DB mid-render.** `sqlite3.DatabaseError` must become an operational error result, never a traceback. (Task 2)
3. **Single-bank, tied, or empty term.** Median/spread of one bank, a 5-way tie at rank 1, and a tracked term with no data must render sensibly (`—`, "1 bank"), not crash on `min()` of empty. (Tasks 2, 3)
4. **Telegram failure leaks the bot token.** `urlopen` errors can embed the request URL; the user message and log must carry only the exception class name. (Task 8)
5. **Double-submit / already running.** Two rapid clicks, two browser tabs, or the scheduled job holding the lock (exit 3) must yield exactly one launch and an "already running" message. (Task 8)
6. **DST/naive datetimes.** NZ formatting across the NZDT→NZST boundary and a naive datetime from a legacy row. (Task 2)

---

## File Structure

**Create (backend):** `src/balance_pricing_engine/pricing_intelligence/health.py`

**Modify (backend):** `pricing_intelligence/service.py` (`_exclude_stale` → `is_stale`; add `get_market_exclusions`), `pricing_intelligence/snapshot.py` (`term_market_from_rates`), `pricing_intelligence/strategy.py` (`apply_manual_competitor_rate_to_config`), `pricing_intelligence/cli.py` (`_health` delegates), `pricing_intelligence/dashboard_data.py` (frames delegate)

**Create (dashboard):** under `src/balance_pricing_engine/dashboard/`:
`settings.py`, `results.py`, `formatting.py`, `models.py`, `services.py`, `actions.py`, `run_worker.py`, `charts.py`, `scenario_text.py`, `pages/__init__.py`, `pages/_common.py`, `pages/overview.py`, `pages/market_watch.py`, `pages/rate_changes.py`, `pages/trends.py`, `pages/scenarios.py`, `pages/scenario_lab.py`, `pages/operations.py`, `pages/system_health.py`

**Modify (dashboard):** `dashboard/app.py` (becomes router)

**Tests (flat, `tests/unit/`):** `conftest.py` (new, fixtures), `dashboard_harness.py`, `test_pricing_health.py`, `test_dashboard_settings.py`, `test_dashboard_formatting.py`, `test_dashboard_services_market.py`, `test_dashboard_services_changes.py`, `test_dashboard_services_history.py`, `test_dashboard_services_scenarios.py`, `test_dashboard_services_health.py`, `test_dashboard_actions.py`, `test_dashboard_charts.py`, `test_dashboard_pages_*.py`, `test_dashboard_boundaries.py`, `test_dashboard_e2e_flows.py`. **Delete in Task 3:** `test_dashboard.py`, `test_dashboard_runtime.py`, `test_dashboard_market_intelligence.py` (see migration map below).

**Docs:** `README.md`, `docs/pricing_intelligence.md` (Dashboard section), `docs/architecture.md` (module map), `AGENTS.md` (dashboard rules).

### Old-test migration map (nothing is dropped silently)

| Old test (file) | Successor |
|---|---|
| market grid/summary/metrics/ranking/changes render (`test_dashboard_market_intelligence`) | Task 3 `test_dashboard_pages_overview.py`, Task 2 `test_dashboard_services_market.py` |
| operational sections render with empty snapshot / no runs / after run / term-deposit section | Task 9 `test_dashboard_pages_system_health.py` |
| history chart not blanked by another product's withdrawal | Task 6 `test_dashboard_services_history.py::test_history_not_blanked_by_term_deposit_withdrawal` |
| freshness not skewed by newer TD run / same-timestamp later run | Task 1 `test_pricing_health.py` (same assertions on `assess_observation_freshness`) |
| guidance with no data; unreadable store keeps scenario controls | Task 2 `test_services_missing_db…`/`corrupt_db…`, Task 3 smoke |
| `_resolve_intelligence_config` x4 | Task 2 `test_dashboard_settings.py` (same four cases) |
| `test_dashboard.py` profit-surface/sensitivity/config-error/stop | Task 7 `test_dashboard_pages_scenario_lab.py` |
| `test_dashboard_runtime.py` stale/partial-failure/unreadable-store | Task 3 + Task 9 + Task 2 (`test_dashboard_e2e_flows.py` flow E) |

---

### Task 1: Backend freshness/health read-model (`health.py`) and delegation

**Files:**
- Create: `src/balance_pricing_engine/pricing_intelligence/health.py`
- Modify: `src/balance_pricing_engine/pricing_intelligence/service.py` (`_exclude_stale` at ~332; add `get_market_exclusions` after `get_market_state`)
- Modify: `src/balance_pricing_engine/pricing_intelligence/dashboard_data.py:21-61,191-264`
- Modify: `src/balance_pricing_engine/pricing_intelligence/cli.py:807-828` (`_health` status block)
- Create: `tests/unit/conftest.py`
- Test: `tests/unit/test_pricing_health.py`

**Interfaces:**
- Consumes: `CollectorHealth`, `RunRecord`, `ObservationStore` (store.py); `CompetitorRateRecord`; `PricingIntelligenceConfig`; `build_telegram_sink_from_env`/`build_webhook_sink_from_env` (alerts.py).
- Produces (all in `health.py`):
  - `class FreshnessState(str, Enum)`: `FRESH`, `STALE`, `UNKNOWN`
  - `class CollectorStatus(str, Enum)`: `FRESH`, `STALE`, `FAILING`, `UNKNOWN`
  - `class HealthLevel(str, Enum)`: `HEALTHY`, `DEGRADED`, `UNKNOWN`
  - `is_stale(captured_at: datetime, now: datetime, max_age_hours: float) -> bool`
  - `collector_status(entry: Optional[CollectorHealth], now: datetime, max_age_hours: Optional[float]) -> CollectorStatus`
  - `@dataclass(frozen=True) ObservationFreshness(bank, state: FreshnessState, oldest_confirmation: Optional[datetime], age_hours: Optional[float], quarantined: bool, not_fully_confirmed: bool)` with `.label -> str` (legacy strings)
  - `assess_observation_freshness(records, bank, *, now, max_age_hours, run_ids: Sequence[str], latest_run_id: Optional[str]) -> ObservationFreshness`
  - `@dataclass(frozen=True) BankHealth(bank, freshness: ObservationFreshness, collector: CollectorStatus, consecutive_failures: int, last_success_at: Optional[datetime], last_error: str, current_record_count: int, health: HealthLevel)`
  - `@dataclass(frozen=True) SystemHealth(evaluated_at, product_type, database_ok: bool, database_issues: tuple[str, ...], latest_run: Optional[RunRecord], banks: tuple[BankHealth, ...], snapshot_valid: bool, telegram_configured: bool, webhook_configured: bool, max_observation_age_hours: float)` with `.fresh_bank_count -> int`
  - `assess_system_health(service, *, product_type=ProductType.HOME_LOAN, now: Optional[datetime]=None) -> SystemHealth`
  - `@dataclass(frozen=True) MarketExclusion(bank, term_months: int, product_name, rate_pct: float, observed_at: datetime, age_hours: float, threshold_hours: float)`
  - Service: `PricingIntelligenceService.get_market_exclusions(product_type=HOME_LOAN, now=None) -> list[MarketExclusion]`
  - Test fixture `market_factory` (tests/unit/conftest.py): see Step 1.

- [ ] **Step 1: Add shared fixtures** — create `tests/unit/conftest.py`:

```python
"""Shared fixtures for dashboard/health tests: a deterministic, controlled NZ market."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.changes import detect_changes
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.store import ObservationStore, RunRecord

NOW = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)
BANKS = ("ANZ", "ASB", "BNZ", "Westpac", "Kiwibank")
BASE_RATES = {"ANZ": 4.89, "ASB": 4.89, "BNZ": 4.85, "Westpac": 4.85, "Kiwibank": 4.89}


@pytest.fixture
def market_factory(tmp_path, make_record):
    """Build a 12-month mortgage market over three runs (NOW-10d, NOW-2d, NOW-1d).

    Run 2 (NOW-1d) moves ANZ 4.89 -> ``anz_new_rate``. Banks in ``stale_banks`` are
    only observed in the first run and then record failed collections, so their last
    observation is 10 days old (older than the 168 h default freshness window).
    """

    def build(*, anz_new_rate: float = 4.79, stale_banks=(), name: str = "market.db"):
        database = tmp_path / name
        config = PricingIntelligenceConfig(database_path=str(database))
        store = ObservationStore(database)
        runs = (
            ("run-0", NOW - timedelta(days=10), dict(BASE_RATES)),
            ("run-1", NOW - timedelta(days=2), dict(BASE_RATES)),
            ("run-2", NOW - timedelta(days=1), {**BASE_RATES, "ANZ": anz_new_rate}),
        )
        previous = None
        for index, (run_id, when, rates) in enumerate(runs):
            live = {b: r for b, r in rates.items() if index == 0 or b not in stale_banks}
            records = [make_record(bank=b, rate_pct=r, observed_at=when) for b, r in live.items()]
            store.record_observations(
                records, run_id=run_id, observed_at=when, product_type=ProductType.HOME_LOAN
            )
            if previous is not None:
                store.record_changes(
                    detect_changes(
                        previous,
                        records,
                        detected_at=when,
                        thresholds=config.validation,
                        run_id=run_id,
                        banks_collected=list(live),
                    )
                )
            for bank in BANKS:
                ok = bank in live
                store.update_collector_health(
                    bank,
                    ProductType.HOME_LOAN,
                    success=ok,
                    error=None if ok else "ConnectionError",
                    at=when,
                    run_id=run_id,
                )
            store.write_run_record(
                RunRecord(
                    run_id=f"op-{run_id}",
                    started_at=when,
                    completed_at=when + timedelta(minutes=2),
                    status="SUCCESS" if len(live) == len(BANKS) else "PARTIAL_SUCCESS",
                    product_type=ProductType.HOME_LOAN,
                    banks_attempted=BANKS,
                    banks_successful=tuple(live),
                    banks_failed=tuple(b for b in BANKS if b not in live),
                    rates_observed=len(live),
                    rates_changed=0,
                    validation_failures=0,
                    suspicious_observations=0,
                    market_state="NORMAL",
                    error_summary=None,
                    collection_run_id=run_id,
                )
            )
            previous = records
        store.close()
        return SimpleNamespace(config=config, database=database, now=NOW, banks=BANKS)

    return build
```

- [ ] **Step 2: Write the failing tests** — `tests/unit/test_pricing_health.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.pricing_intelligence.health import (
    CollectorStatus,
    FreshnessState,
    HealthLevel,
    assess_observation_freshness,
    assess_system_health,
    collector_status,
    is_stale,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import CollectorHealth, ObservationStore

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def _health(failures=0, success_age_h=1.0):
    return CollectorHealth(
        bank="ANZ", product_type="home_loan", consecutive_failures=failures,
        last_success_at=NOW - timedelta(hours=success_age_h), last_failure_at=None,
        last_error=None, last_alert_sent_at=None,
    )


def test_is_stale_is_strictly_older_than_the_window():
    assert not is_stale(NOW - timedelta(hours=168), NOW, 168)
    assert is_stale(NOW - timedelta(hours=168, seconds=1), NOW, 168)


@pytest.mark.parametrize(
    "entry,max_age,expected",
    [
        (None, 168, CollectorStatus.UNKNOWN),
        (_health(failures=2), 168, CollectorStatus.FAILING),
        (_health(success_age_h=200), 168, CollectorStatus.STALE),
        (_health(success_age_h=200), None, CollectorStatus.FRESH),
        (_health(), 168, CollectorStatus.FRESH),
    ],
)
def test_collector_status(entry, max_age, expected):
    assert collector_status(entry, NOW, max_age) is expected


def test_observation_freshness_states(make_record):
    fresh = [make_record(bank="ANZ", observed_at=NOW - timedelta(hours=5))]
    stale = [make_record(bank="ANZ", observed_at=NOW - timedelta(hours=400))]
    kwargs = dict(now=NOW, max_age_hours=168, run_ids=["r1"], latest_run_id="r1")
    assert assess_observation_freshness(fresh, "ANZ", **kwargs).state is FreshnessState.FRESH
    assert assess_observation_freshness(stale, "ANZ", **kwargs).state is FreshnessState.STALE
    missing = assess_observation_freshness([], "ANZ", **kwargs)
    assert missing.state is FreshnessState.UNKNOWN and missing.label == "Missing"


def test_observation_freshness_flags_a_bank_not_touched_by_the_latest_run(make_record):
    records = [make_record(bank="ANZ", observed_at=NOW - timedelta(hours=5))]
    result = assess_observation_freshness(
        records, "ANZ", now=NOW, max_age_hours=168, run_ids=["old-run"], latest_run_id="new-run"
    )
    assert result.not_fully_confirmed
    assert result.label == "Fresh; not fully confirmed in latest stored run"


def test_observation_freshness_quarantine_wins(make_record):
    records = [make_record(bank="ANZ", observed_at=NOW, validation_status="warning")]
    result = assess_observation_freshness(
        records, "ANZ", now=NOW, max_age_hours=168, run_ids=["r"], latest_run_id="r"
    )
    assert result.quarantined and result.label == "Quarantined readings"


def test_system_health_for_a_healthy_market(market_factory):
    market = market_factory()
    with PricingIntelligenceService(
        config=market.config, store=ObservationStore(market.database)
    ) as service:
        health = assess_system_health(service, now=market.now)
    assert health.database_ok
    assert health.snapshot_valid
    assert health.fresh_bank_count == 5
    assert health.latest_run.status == "SUCCESS"
    assert all(b.health is HealthLevel.HEALTHY for b in health.banks)


def test_system_health_marks_a_failed_bank_stale_and_degraded(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    with PricingIntelligenceService(
        config=market.config, store=ObservationStore(market.database)
    ) as service:
        health = assess_system_health(service, now=market.now)
    westpac = next(b for b in health.banks if b.bank == "Westpac")
    assert westpac.freshness.state is FreshnessState.STALE
    assert westpac.health is HealthLevel.DEGRADED
    assert westpac.consecutive_failures == 2
    assert health.fresh_bank_count == 4


def test_market_exclusions_explain_why_a_bank_dropped_out(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    with PricingIntelligenceService(
        config=market.config, store=ObservationStore(market.database)
    ) as service:
        exclusions = service.get_market_exclusions(now=market.now)
        snapshot = service.get_market_snapshot(now=market.now)
    assert [e.bank for e in exclusions] == ["Westpac"]
    assert exclusions[0].term_months == 12 and exclusions[0].threshold_hours == 168.0
    assert exclusions[0].age_hours == pytest.approx(240.0)
    assert "Westpac" not in snapshot.banks
```

- [ ] **Step 3: Run to verify failure**

Run: `python3 -m pytest tests/unit/test_pricing_health.py -v`
Expected: FAIL — `ModuleNotFoundError: ...pricing_intelligence.health`. (If a fixture error appears instead — e.g. `record_observations` arguments or `detect_changes` signature — fix the fixture first; the plan's signatures were read from source but never executed.)

- [ ] **Step 4: Implement `health.py`**

```python
"""Single source of truth for freshness, collector health and market exclusions.

Before this module the same threshold logic lived in ``cli._health``,
``dashboard_data`` and ``service._exclude_stale``. Everything that needs to say
"fresh", "stale" or "excluded because stale" -- the CLI, the dashboard, the
market snapshot -- goes through here so they cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Optional, Sequence

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.alerts import (
    build_telegram_sink_from_env,
    build_webhook_sink_from_env,
)
from balance_pricing_engine.pricing_intelligence.store import CollectorHealth, RunRecord

if TYPE_CHECKING:  # service imports this module; avoid the cycle at runtime
    from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService


class FreshnessState(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class CollectorStatus(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    FAILING = "FAILING"
    UNKNOWN = "UNKNOWN"


class HealthLevel(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


def is_stale(captured_at: datetime, now: datetime, max_age_hours: float) -> bool:
    """True when ``captured_at`` is strictly older than the freshness window."""
    return captured_at < now - timedelta(hours=max_age_hours)


def collector_status(
    entry: Optional[CollectorHealth], now: datetime, max_age_hours: Optional[float]
) -> CollectorStatus:
    """A collector's status. ``max_age_hours=None`` disables the staleness check."""
    if entry is None:
        return CollectorStatus.UNKNOWN
    if entry.consecutive_failures > 0:
        return CollectorStatus.FAILING
    if (
        max_age_hours is not None
        and entry.last_success_at is not None
        and is_stale(entry.last_success_at, now, max_age_hours)
    ):
        return CollectorStatus.STALE
    return CollectorStatus.FRESH


@dataclass(frozen=True)
class ObservationFreshness:
    bank: str
    state: FreshnessState
    oldest_confirmation: Optional[datetime]
    age_hours: Optional[float]
    quarantined: bool
    not_fully_confirmed: bool

    @property
    def label(self) -> str:
        """Legacy human label (kept byte-identical for the existing freshness table)."""
        if self.quarantined:
            return "Quarantined readings"
        text = {
            FreshnessState.UNKNOWN: "Missing",
            FreshnessState.STALE: "Stale",
            FreshnessState.FRESH: "Fresh",
        }[self.state]
        if self.not_fully_confirmed:
            text += "; not fully confirmed in latest stored run"
        return text


def assess_observation_freshness(
    records: Sequence[CompetitorRateRecord],
    bank: str,
    *,
    now: datetime,
    max_age_hours: float,
    run_ids: Sequence[str],
    latest_run_id: Optional[str],
) -> ObservationFreshness:
    """Freshness of one bank's current observations (oldest confirmed reading).

    "Not fully confirmed" is decided by run identity, not by comparing timestamps:
    two runs can share a ``started_at``.
    """
    bank_records = [r for r in records if r.bank == bank]
    confirmed = [r for r in bank_records if r.validation_status != "warning"]
    oldest = min((r.captured_at_utc for r in confirmed), default=None)
    age = (now - oldest).total_seconds() / 3600 if oldest is not None else None
    if age is None:
        state = FreshnessState.UNKNOWN
    elif age > max_age_hours:
        state = FreshnessState.STALE
    else:
        state = FreshnessState.FRESH
    quarantined = any(r.validation_status == "warning" for r in bank_records)
    not_confirmed = (
        not quarantined
        and oldest is not None
        and latest_run_id is not None
        and any(run_id != latest_run_id for run_id in run_ids)
    )
    return ObservationFreshness(bank, state, oldest, age, quarantined, not_confirmed)


@dataclass(frozen=True)
class BankHealth:
    bank: str
    freshness: ObservationFreshness
    collector: CollectorStatus
    consecutive_failures: int
    last_success_at: Optional[datetime]
    last_error: str
    current_record_count: int
    health: HealthLevel


@dataclass(frozen=True)
class SystemHealth:
    evaluated_at: datetime
    product_type: ProductType
    database_ok: bool
    database_issues: tuple[str, ...]
    latest_run: Optional[RunRecord]
    banks: tuple[BankHealth, ...]
    snapshot_valid: bool
    telegram_configured: bool
    webhook_configured: bool
    max_observation_age_hours: float

    @property
    def fresh_bank_count(self) -> int:
        return sum(1 for b in self.banks if b.freshness.state is FreshnessState.FRESH)


@dataclass(frozen=True)
class MarketExclusion:
    """A comparable record the market snapshot dropped because it is stale."""

    bank: str
    term_months: int
    product_name: str
    rate_pct: float
    observed_at: datetime
    age_hours: float
    threshold_hours: float


def _bank_health_level(freshness: ObservationFreshness, collector: CollectorStatus) -> HealthLevel:
    if freshness.state is FreshnessState.UNKNOWN and collector is CollectorStatus.UNKNOWN:
        return HealthLevel.UNKNOWN
    if (
        freshness.state is FreshnessState.FRESH
        and collector is CollectorStatus.FRESH
        and not freshness.quarantined
    ):
        return HealthLevel.HEALTHY
    return HealthLevel.DEGRADED


def assess_system_health(
    service: "PricingIntelligenceService",
    *,
    product_type: ProductType | str = ProductType.HOME_LOAN,
    now: Optional[datetime] = None,
) -> SystemHealth:
    """Everything an operator needs to judge whether Pricing Watch is healthy."""
    product = ProductType(product_type)
    now = now or datetime.now(timezone.utc)
    config = service.config
    store = service.store
    report = store.check_integrity(thresholds=config.validation)
    records = store.get_current_observations(product_type=product)
    run_ids_by_bank = store.get_current_observation_run_ids(product_type=product)
    latest_run_id = store.latest_run_id(product_type=product)
    collectors = {c.bank: c for c in store.get_collector_health(product_type=product)}

    banks = []
    for bank in config.selected_banks:
        freshness = assess_observation_freshness(
            records, bank, now=now, max_age_hours=config.max_observation_age_hours,
            run_ids=run_ids_by_bank.get(bank, []), latest_run_id=latest_run_id,
        )
        entry = collectors.get(bank)
        status = collector_status(entry, now, config.max_observation_age_hours)
        banks.append(
            BankHealth(
                bank=bank,
                freshness=freshness,
                collector=status,
                consecutive_failures=entry.consecutive_failures if entry else 0,
                last_success_at=entry.last_success_at if entry else None,
                last_error=(entry.last_error or "") if entry else "",
                current_record_count=sum(1 for r in records if r.bank == bank),
                health=_bank_health_level(freshness, status),
            )
        )
    snapshot = service.get_market_snapshot(product_type=product, now=now)
    return SystemHealth(
        evaluated_at=now,
        product_type=product,
        database_ok=report.ok,
        database_issues=tuple(report.issues),
        latest_run=store.get_latest_run_record(product_type=product),
        banks=tuple(banks),
        snapshot_valid=bool(snapshot.terms),
        telegram_configured=build_telegram_sink_from_env() is not None,
        webhook_configured=build_webhook_sink_from_env() is not None,
        max_observation_age_hours=config.max_observation_age_hours,
    )
```

- [ ] **Step 5: Wire the service** — in `service.py`, add `from balance_pricing_engine.pricing_intelligence.health import MarketExclusion, is_stale` to the imports, replace the `_exclude_stale` body (keep its docstring):

```python
    cutoff_hours = max_age_hours
    return [r for r in records if not is_stale(r.captured_at_utc, now, cutoff_hours)]
```

(delete the now-unused `cutoff = now - timedelta(...)` line) and add after `get_market_state`:

```python
    def get_market_exclusions(
        self,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        now: Optional[datetime] = None,
    ) -> list[MarketExclusion]:
        """Comparable records ``get_market_snapshot`` drops for staleness.

        Same record set and the same predicate as the snapshot, so the
        explanation cannot diverge from the exclusion.
        """
        now = now or datetime.now(timezone.utc)
        rate_category = _resolve_rate_category(product_type, None)
        records = self.store.get_current_observations(product_type=product_type)
        records = _resolve_suspicious_if_present(
            records,
            lambda: self.store.get_last_confirmed_observations(product_type=product_type),
        )
        threshold = self.config.max_observation_age_hours
        return [
            MarketExclusion(
                bank=r.bank,
                term_months=r.term_months_normalised,
                product_name=r.product_name,
                rate_pct=r.advertised_rate_pct,
                observed_at=r.captured_at_utc,
                age_hours=(now - r.captured_at_utc).total_seconds() / 3600,
                threshold_hours=threshold,
            )
            for r in records
            if is_stale(r.captured_at_utc, now, threshold)
            and r.rate_category == rate_category
            and r.bank in self.config.selected_banks
            and r.term_months_normalised in self.config.tracked_terms_months
        ]
```

- [ ] **Step 6: Run the new tests**

Run: `python3 -m pytest tests/unit/test_pricing_health.py -v`
Expected: PASS (all). If `test_system_health_*` fails on `consecutive_failures == 2`, check how `update_collector_health` counts retried `run_id`s (its docstring describes retry-dedup) and adjust the fixture's expected count — do not loosen the production code.

- [ ] **Step 7: Delegate the CLI and frames (behaviour-preserving)** — `dashboard_data.py`: add `from balance_pricing_engine.pricing_intelligence.health import assess_observation_freshness, collector_status`; replace the loop body of `build_freshness_frame` with:

```python
    rows = []
    for bank in banks:
        freshness = assess_observation_freshness(
            records, bank, now=now, max_age_hours=max_age_hours,
            run_ids=(run_ids_by_bank or {}).get(bank, []), latest_run_id=latest_run_id,
        )
        rows.append({
            "Bank": bank,
            "Oldest confirmation (UTC)": freshness.oldest_confirmation,
            "Age (hours)": freshness.age_hours,
            "Status": freshness.label,
        })
    return pd.DataFrame(rows).set_index("Bank")
```

and in `build_collector_health_frame` delete the local `_status`/`max_age` and use `collector_status(entry, evaluated_at, max_observation_age_hours).value` for the `"Status"` cell (keep the `UNKNOWN` row for banks with no entry as-is). In `cli._health`, replace the `if health.consecutive_failures > 0 … else FRESH` block with:

```python
        state = collector_status(health, now, service.config.max_observation_age_hours)
        status = (
            f"FAILING ({health.consecutive_failures}x)"
            if state is CollectorStatus.FAILING
            else state.value
        )
```

(import `collector_status`, `CollectorStatus` from `health`; drop the unused `max_age` local and unused `timedelta` import only if nothing else uses them.)

- [ ] **Step 8: Regression net**

Run: `python3 -m pytest tests/unit/test_pricing_intelligence_dashboard_data.py tests/unit/test_pricing_intelligence_cli.py tests/unit/test_pricing_intelligence_service.py tests/unit/test_pricing_intelligence_boundaries.py tests/unit/test_dashboard_market_intelligence.py -q`
Expected: all PASS, unchanged counts. Then `python3 -m ruff check src tests/unit/test_pricing_health.py tests/unit/conftest.py` and `python3 -m ruff format --check src/balance_pricing_engine/pricing_intelligence/health.py tests/unit/test_pricing_health.py tests/unit/conftest.py` — Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add src/balance_pricing_engine/pricing_intelligence tests/unit/conftest.py tests/unit/test_pricing_health.py
git commit -m "refactor: single freshness/health read-model shared by CLI, snapshot and dashboard" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

**Acceptance:** one implementation of the staleness/collector rule; CLI `health` output and dashboard frames byte-identical; `get_market_exclusions` explains exactly what the snapshot drops.

---

### Task 2: Settings, results, formatting, view models, and the market services seam

**Files:**
- Create: `dashboard/settings.py`, `dashboard/results.py`, `dashboard/formatting.py`, `dashboard/models.py`, `dashboard/services.py`
- Modify: `pricing_intelligence/snapshot.py` (add `term_market_from_rates`)
- Test: `tests/unit/test_dashboard_settings.py`, `test_dashboard_formatting.py`, `test_dashboard_services_market.py`, `test_pricing_intelligence_snapshot.py` (append)

**Interfaces:**
- Consumes (Task 1): `assess_system_health`, `SystemHealth`, `MarketExclusion`, `FreshnessState`; `PricingIntelligenceService`, `TermMarket`, `MarketSnapshot`, `rate_change_bps`, `MarketStateAssessment`; fixture `market_factory`.
- Produces:
  - `snapshot.term_market_from_rates(term_months: int, rates: Mapping[str, float], product_type=ProductType.HOME_LOAN) -> TermMarket`
  - `settings.DashboardSettings(intelligence: PricingIntelligenceConfig, engine_config_path: Path, engine_config_text: str, backup_dir: Path, repo_root: Path)`; `settings.resolve_intelligence_config(config_yaml: str) -> PricingIntelligenceConfig`; `settings.load_settings(env: Optional[Mapping[str,str]] = None, *, database_path: Optional[str] = None) -> DashboardSettings`
  - `results.Result[T]` frozen dataclass: `.status` in `"OK"|"EMPTY"|"ERROR"`, `.value`, `.message`; `Result.ok(v)`, `Result.empty(msg)`, `Result.error(msg)`; `.is_ok`
  - `formatting`: `fmt_rate_pct(v: Optional[float], places=2)`, `fmt_rate_fraction(v: Optional[float])`, `fmt_bps(v: Optional[float])`, `fmt_nz_datetime(dt: Optional[datetime])`, `fmt_age_hours(h: Optional[float])`, `fmt_term(months: Optional[int])`, `fmt_freshness(state: FreshnessState)`, `fmt_health(level: HealthLevel)`, `NZ_TZ`
  - `models`: `HeaderInfo`, `MatrixCell`, `MarketMatrix`, `RankedBank`, `TermSummary`, `BankPosition`, `Overview` (fields below)
  - `services.DashboardServices(settings, *, clock=…)` with `.settings`, `.header() -> Result[HeaderInfo]`, `.overview() -> Result[Overview]`, `.term_summary(term_months: int) -> Result[TermSummary]`, `.bank_positions(term_months: int) -> Result[tuple[BankPosition, ...]]`, `.db_stamp() -> tuple[str, int]`

- [ ] **Step 1: Snapshot helper (RED→GREEN)** — append to `tests/unit/test_pricing_intelligence_snapshot.py`:

```python
def test_term_market_from_rates_matches_snapshot_semantics_including_ties():
    from balance_pricing_engine.pricing_intelligence.snapshot import term_market_from_rates

    market = term_market_from_rates(
        12, {"ANZ": 4.79, "BNZ": 4.85, "Westpac": 4.85, "ASB": 4.89, "Kiwibank": 4.89}
    )
    assert [(e.bank, e.rank) for e in market.ranking()] == [
        ("ANZ", 1), ("BNZ", 2), ("Westpac", 2), ("ASB", 4), ("Kiwibank", 4),
    ]
    assert market.best_rate == 4.79 and market.median_rate == 4.85
    assert market.spread_bps == 10.0
```

Run `python3 -m pytest tests/unit/test_pricing_intelligence_snapshot.py -q` → FAIL (ImportError). Implement in `snapshot.py` (below `build_market_snapshot`):

```python
def term_market_from_rates(
    term_months: int,
    rates: Mapping[str, float],
    product_type: ProductType | str = ProductType.HOME_LOAN,
) -> TermMarket:
    """Build a ``TermMarket`` from already-resolved ``{bank: rate_pct}``.

    For history charts: per-date market statistics come from the same ranking and
    dispersion code the live snapshot uses, never from UI-side arithmetic.
    """
    product = ProductType(product_type)
    lower_is_better = _better_is_lower(product)
    ordered = sorted(rates.items(), key=lambda kv: (kv[1] if lower_is_better else -kv[1], kv[0]))
    ranks = _competition_ranks([rate for _, rate in ordered])
    entries = tuple(
        BankTermRate(
            bank=bank, rate_pct=rate, rank=rank, product_name="", rate_category="",
            source_url="", observed_at=datetime.min.replace(tzinfo=timezone.utc),
        )
        for (bank, rate), rank in zip(ordered, ranks)
    )
    return TermMarket(term_months=term_months, product_type=product, entries=entries)
```

(add `Mapping` to the `typing` import). Re-run → PASS.

- [ ] **Step 2: Settings tests** — `tests/unit/test_dashboard_settings.py` (these are the four ported `_resolve_intelligence_config` cases plus loader behaviour):

```python
from pathlib import Path

import yaml

from balance_pricing_engine.dashboard.settings import load_settings, resolve_intelligence_config
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig

ROOT = Path(__file__).resolve().parents[2]


def _yaml(**intel):
    base = yaml.safe_load((ROOT / "configs/base.yaml").read_text())
    base["pricing_intelligence"] = intel
    return yaml.safe_dump(base)


def test_resolve_pulls_settings_from_the_yaml():
    cfg = resolve_intelligence_config(_yaml(selected_banks=["ANZ", "ASB"], database_path="x.db"))
    assert cfg.selected_banks == ["ANZ", "ASB"] and cfg.database_path == "x.db"


def test_resolve_falls_back_to_defaults_on_invalid_yaml(monkeypatch):
    monkeypatch.delenv("PRICING_INTELLIGENCE_DB_PATH", raising=False)
    assert resolve_intelligence_config("random_seed: [") == PricingIntelligenceConfig()


def test_resolve_falls_back_to_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PRICING_INTELLIGENCE_DB_PATH", str(tmp_path / "env.db"))
    assert resolve_intelligence_config("random_seed: [").database_path == str(tmp_path / "env.db")


def test_resolve_yaml_database_path_wins_over_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PRICING_INTELLIGENCE_DB_PATH", str(tmp_path / "env.db"))
    assert resolve_intelligence_config(_yaml(database_path="yaml.db")).database_path == "yaml.db"


def test_load_settings_defaults_to_base_yaml_and_overrides_database(tmp_path):
    settings = load_settings({}, database_path=str(tmp_path / "t.db"))
    assert settings.engine_config_path == ROOT / "configs" / "base.yaml"
    assert settings.intelligence.database_path == str(tmp_path / "t.db")
    assert settings.backup_dir == ROOT / "data" / "processed" / "pricing_intelligence" / "backups"
```

Run → FAIL. Implement `settings.py`:

```python
"""Dashboard settings: which engine config, which observation database."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

import yaml

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.pricing_intelligence.config import (
    PricingIntelligenceConfig,
    resolve_database_path_from_env,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_ENV_VAR = "PRICING_WATCH_CONFIG"


@dataclass(frozen=True)
class DashboardSettings:
    intelligence: PricingIntelligenceConfig
    engine_config_path: Path
    engine_config_text: str
    backup_dir: Path
    repo_root: Path


def resolve_intelligence_config(config_yaml: str) -> PricingIntelligenceConfig:
    """``pricing_intelligence`` settings from an EngineConfig YAML, else defaults.

    An explicit ``database_path`` in the YAML wins; otherwise the
    ``PRICING_INTELLIGENCE_DB_PATH`` override applies exactly as in the CLI and the
    scheduled workflow, so all three read the same database.
    """
    env_default = resolve_database_path_from_env(PricingIntelligenceConfig())
    try:
        engine_config = EngineConfig(**yaml.safe_load(config_yaml))
    except Exception:  # noqa: BLE001 - any bad YAML falls back to defaults
        return env_default
    configured = engine_config.pricing_intelligence
    if configured is None:
        return env_default
    if "database_path" in configured.model_fields_set:
        return configured
    return configured.model_copy(update={"database_path": env_default.database_path})


def load_settings(
    env: Optional[Mapping[str, str]] = None, *, database_path: Optional[str] = None
) -> DashboardSettings:
    env = os.environ if env is None else env
    path = Path(env.get(CONFIG_ENV_VAR) or REPO_ROOT / "configs" / "base.yaml")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    intelligence = resolve_intelligence_config(text)
    if database_path is not None:
        intelligence = intelligence.model_copy(update={"database_path": database_path})
    return DashboardSettings(
        intelligence=intelligence,
        engine_config_path=path,
        engine_config_text=text,
        backup_dir=REPO_ROOT / "data" / "processed" / "pricing_intelligence" / "backups",
        repo_root=REPO_ROOT,
    )
```

Run → PASS.

- [ ] **Step 3: Formatting tests** — `tests/unit/test_dashboard_formatting.py`:

```python
from datetime import datetime, timezone

import pytest

from balance_pricing_engine.dashboard.formatting import (
    fmt_age_hours, fmt_bps, fmt_freshness, fmt_nz_datetime, fmt_rate_fraction, fmt_rate_pct,
    fmt_term,
)
from balance_pricing_engine.pricing_intelligence.health import FreshnessState


def test_rates_keep_units_explicit():
    assert fmt_rate_pct(4.85) == "4.85%"
    assert fmt_rate_fraction(0.0485) == "4.85%"
    assert fmt_rate_pct(None) == "—" and fmt_rate_fraction(None) == "—"


@pytest.mark.parametrize(
    "value,text",
    [(-10.0, "-10 bps"), (4.0, "+4 bps"), (0.0, "0 bps"), (2.5, "+2.5 bps"), (None, "—")],
)
def test_bps(value, text):
    assert fmt_bps(value) == text


def test_nz_datetime_converts_from_utc_with_dst():
    # 2026-10-10 20:03 UTC is 09:03 NZDT on 11 Oct (DST began 27 Sep 2026)
    assert fmt_nz_datetime(datetime(2026, 10, 10, 20, 3, tzinfo=timezone.utc)) == "11 Oct 2026 09:03"
    # Winter: UTC+12
    assert fmt_nz_datetime(datetime(2026, 6, 30, 20, 3, tzinfo=timezone.utc)) == "01 Jul 2026 08:03"


def test_nz_datetime_treats_naive_values_as_utc_and_handles_none():
    assert fmt_nz_datetime(datetime(2026, 6, 30, 20, 3)) == "01 Jul 2026 08:03"
    assert fmt_nz_datetime(None) == "—"


def test_age_terms_and_freshness_labels():
    assert fmt_age_hours(41.2) == "41 h" and fmt_age_hours(None) == "—"
    assert [fmt_term(m) for m in (6, 12, 18, 24, 60)] == ["6M", "1Y", "18M", "2Y", "5Y"]
    assert fmt_freshness(FreshnessState.STALE) == "⚠ STALE"
    assert fmt_freshness(FreshnessState.FRESH) == "FRESH"
```

Run → FAIL. Implement `formatting.py`:

```python
"""Pure presentation helpers. They format; they never round or recompute values."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from balance_pricing_engine.pricing_intelligence.health import FreshnessState, HealthLevel
from balance_pricing_engine.pricing_intelligence.snapshot import _term_label

NZ_TZ = ZoneInfo("Pacific/Auckland")
EM_DASH = "—"


def fmt_rate_pct(value: Optional[float], places: int = 2) -> str:
    """Advertised-percent rate (``4.85`` -> ``4.85%``)."""
    return EM_DASH if value is None else f"{value:.{places}f}%"


def fmt_rate_fraction(value: Optional[float], places: int = 2) -> str:
    """Decimal-fraction rate (``0.0485`` -> ``4.85%``), as the optimiser reports."""
    return EM_DASH if value is None else f"{value * 100:.{places}f}%"


def fmt_bps(value: Optional[float]) -> str:
    if value is None:
        return EM_DASH
    if value == 0:
        return "0 bps"
    text = f"{value:+.0f}" if float(value).is_integer() else f"{value:+.1f}"
    return f"{text} bps"


def fmt_nz_datetime(value: Optional[datetime]) -> str:
    if value is None:
        return EM_DASH
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(NZ_TZ).strftime("%d %b %Y %H:%M")


def fmt_age_hours(hours: Optional[float]) -> str:
    return EM_DASH if hours is None else f"{hours:.0f} h"


def fmt_term(months: Optional[int]) -> str:
    return "Floating" if months is None else _term_label(months)


def fmt_freshness(state: FreshnessState) -> str:
    return "⚠ STALE" if state is FreshnessState.STALE else state.value


def fmt_health(level: HealthLevel) -> str:
    return "⚠ DEGRADED" if level is HealthLevel.DEGRADED else level.value
```

Run → PASS.

- [ ] **Step 4: Results and models** — create `dashboard/results.py`:

```python
"""A tiny tagged result so pages render EMPTY/ERROR states instead of tracebacks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, Optional, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Result(Generic[T]):
    status: Literal["OK", "EMPTY", "ERROR"]
    value: Optional[T] = None
    message: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status == "OK"

    @classmethod
    def ok(cls, value: T) -> "Result[T]":
        return cls("OK", value)

    @classmethod
    def empty(cls, message: str) -> "Result[T]":
        return cls("EMPTY", None, message)

    @classmethod
    def error(cls, message: str) -> "Result[T]":
        return cls("ERROR", None, message)
```

Create `dashboard/models.py` (market-side view models; later tasks append their own):

```python
"""Frozen view models returned by DashboardServices. No behaviour, no Streamlit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional

from balance_pricing_engine.pricing_intelligence.health import FreshnessState, MarketExclusion


@dataclass(frozen=True)
class HeaderInfo:
    last_update: Optional[datetime]  # latest run's completed_at
    market_state: Optional[str]  # backend MarketState value
    fresh_banks: int
    configured_banks: int


@dataclass(frozen=True)
class MatrixCell:
    rate_pct: Optional[float]
    is_leader: bool = False
    stale: bool = False
    rate_category: Optional[str] = None


@dataclass(frozen=True)
class MarketMatrix:
    banks: tuple[str, ...]
    terms: tuple[int, ...]
    cells: Mapping[tuple[str, int], MatrixCell]


@dataclass(frozen=True)
class RankedBank:
    rank: int
    bank: str
    rate_pct: float
    product_name: str


@dataclass(frozen=True)
class TermSummary:
    term_months: int
    leaders: tuple[str, ...]
    lowest_pct: float
    median_pct: float
    highest_pct: float
    spread_bps: float
    banks_observed: int
    fresh_banks: int
    ranking: tuple[RankedBank, ...]


@dataclass(frozen=True)
class BankPosition:
    bank: str
    rate_pct: float
    rank: int
    vs_leader_bps: float
    vs_median_bps: float
    last_change_at: Optional[datetime]
    freshness: FreshnessState


@dataclass(frozen=True)
class Overview:
    header: HeaderInfo
    changes_today: int
    changes_7d: int
    banks_repricing_7d: int
    dominant_direction: str
    most_active_term: Optional[int]
    market_state_rationale: str
    matrix: MarketMatrix
    default_term: Optional[int]
    exclusions: tuple[MarketExclusion, ...]
```

- [ ] **Step 5: Services tests (market side)** — `tests/unit/test_dashboard_services_market.py`:

```python
import sqlite3

import pytest

from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.pricing_intelligence.health import FreshnessState
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore


def make_services(market):
    settings = load_settings({}, database_path=str(market.database))
    return DashboardServices(settings, clock=lambda: market.now)


def backend_snapshot(market):
    with PricingIntelligenceService(
        config=market.config, store=ObservationStore(market.database)
    ) as service:
        return service.get_market_snapshot(now=market.now)


def test_term_summary_equals_backend_values_and_ties(market_factory):
    market = market_factory()
    result = make_services(market).term_summary(12)
    truth = backend_snapshot(market).term(12)
    summary = result.value
    assert result.is_ok
    assert summary.leaders == truth.market_leaders == ("ANZ",)
    assert (summary.lowest_pct, summary.median_pct, summary.highest_pct) == (
        truth.lowest_rate, truth.median_rate, truth.highest_rate,
    ) == (4.79, 4.85, 4.89)
    assert summary.spread_bps == truth.spread_bps == 10.0
    assert [(r.rank, r.bank) for r in summary.ranking] == [
        (1, "ANZ"), (2, "BNZ"), (2, "Westpac"), (4, "ASB"), (4, "Kiwibank"),
    ]
    assert summary.banks_observed == 5 and summary.fresh_banks == 5


def test_overview_matrix_marks_leader_and_stale_bank(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    overview = make_services(market).overview().value
    cell = overview.matrix.cells
    assert cell[("ANZ", 12)].is_leader and cell[("ANZ", 12)].rate_pct == 4.79
    # stale: shown, marked, and not part of the live ranking
    assert cell[("Westpac", 12)].stale and cell[("Westpac", 12)].rate_pct == 4.85
    assert [e.bank for e in overview.exclusions] == ["Westpac"]
    assert overview.header.fresh_banks == 4 and overview.header.configured_banks == 5
    assert overview.changes_7d >= 1 and overview.dominant_direction == "DOWN"
    assert overview.default_term == 12


def test_bank_positions_use_backend_bps(market_factory):
    market = market_factory()
    positions = {p.bank: p for p in make_services(market).bank_positions(12).value}
    asb = positions["ASB"]
    assert (asb.rank, asb.vs_leader_bps, asb.vs_median_bps) == (4, 10.0, 4.0)
    assert asb.freshness is FreshnessState.FRESH
    assert positions["ANZ"].last_change_at is not None
    assert positions["ASB"].last_change_at is None


def test_single_bank_term_has_zero_spread_and_no_crash(market_factory):
    market = market_factory(stale_banks=("ANZ", "ASB", "BNZ", "Westpac"))  # only Kiwibank live
    summary = make_services(market).term_summary(12).value
    assert summary.banks_observed == 1 and summary.spread_bps == 0.0
    assert summary.leaders == ("Kiwibank",)


def test_unknown_term_is_an_empty_result(market_factory):
    result = make_services(market_factory()).term_summary(99)
    assert result.status == "EMPTY" and "99" in result.message


def test_missing_database_is_empty_and_not_created(tmp_path):
    missing = tmp_path / "nope.db"
    services = DashboardServices(load_settings({}, database_path=str(missing)))
    result = services.overview()
    assert result.status == "EMPTY" and "collect" in result.message.lower()
    assert not missing.exists()


def test_corrupt_database_is_an_operational_error_not_a_traceback(tmp_path, caplog):
    bad = tmp_path / "bad.db"
    bad.write_text("not a SQLite database")
    result = DashboardServices(load_settings({}, database_path=str(bad))).overview()
    assert result.status == "ERROR"
    assert "unavailable" in result.message.lower()
    assert "Traceback" not in result.message and "sqlite" not in result.message.lower()
    assert any(r.exc_info for r in caplog.records)  # detail went to the log


def test_empty_database_is_an_empty_result(tmp_path):
    empty = tmp_path / "empty.db"
    ObservationStore(empty).close()
    assert DashboardServices(load_settings({}, database_path=str(empty))).overview().status == "EMPTY"
```

Run → FAIL (no `services`).

- [ ] **Step 6: Implement `services.py` (market side)**

```python
"""Thin read-model over the pricing-intelligence service.

Every number shown in the dashboard comes from here, and every number here comes
from a backend call. No rankings, bps, freshness or market statistics are derived
in this module -- see tests/unit/test_dashboard_boundaries.py.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, TypeVar

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard.models import (
    BankPosition, HeaderInfo, MarketMatrix, MatrixCell, Overview, RankedBank, TermSummary,
)
from balance_pricing_engine.dashboard.results import Result
from balance_pricing_engine.dashboard.settings import DashboardSettings
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.health import (
    FreshnessState, assess_system_health,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

logger = logging.getLogger(__name__)
T = TypeVar("T")

NO_DATA_MESSAGE = (
    "No market data collected yet. Open Operations and run Pricing Watch, or run the "
    "collector from the CLI (see docs/pricing_intelligence.md)."
)
DB_UNAVAILABLE_MESSAGE = (
    "Market database unavailable. Stored data cannot be read right now; "
    "see System Health and the application log."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _MissingDatabase(Exception):
    pass


class DashboardServices:
    def __init__(
        self, settings: DashboardSettings, *, clock: Callable[[], datetime] = _utcnow
    ) -> None:
        self.settings = settings
        self._clock = clock

    # ------------------------------------------------------------ plumbing

    def db_stamp(self) -> tuple[str, int]:
        """(path, mtime_ns) -- a cache key that changes when the store changes."""
        path = Path(self.settings.intelligence.database_path)
        try:
            return (str(path), path.stat().st_mtime_ns)
        except OSError:
            return (str(path), 0)

    @contextmanager
    def _service(self) -> Iterator[PricingIntelligenceService]:
        path = Path(self.settings.intelligence.database_path)
        if not path.exists():  # ObservationStore would silently create it
            raise _MissingDatabase(str(path))
        service = PricingIntelligenceService(
            config=self.settings.intelligence, store=ObservationStore(path)
        )
        try:
            yield service
        finally:
            service.close()

    def _guard(self, fn: Callable[[PricingIntelligenceService], Result[T]]) -> Result[T]:
        try:
            with self._service() as service:
                return fn(service)
        except _MissingDatabase:
            return Result.empty(NO_DATA_MESSAGE)
        except (sqlite3.Error, OSError):
            logger.exception("market database read failed")
            return Result.error(DB_UNAVAILABLE_MESSAGE)
        except Exception:  # noqa: BLE001 - last resort: log detail, show one sentence
            logger.exception("unexpected dashboard service failure")
            return Result.error("Something went wrong reading market data; see the log.")

    # -------------------------------------------------------------- reads

    def header(self) -> Result[HeaderInfo]:
        def build(service: PricingIntelligenceService) -> Result[HeaderInfo]:
            now = self._clock()
            health = assess_system_health(service, now=now)
            state = service.get_market_state(now=now)
            run = health.latest_run
            return Result.ok(
                HeaderInfo(
                    last_update=run.completed_at if run else None,
                    market_state=state.state.value,
                    fresh_banks=health.fresh_bank_count,
                    configured_banks=len(health.banks),
                )
            )

        return self._guard(build)

    def overview(self) -> Result[Overview]:
        def build(service: PricingIntelligenceService) -> Result[Overview]:
            now = self._clock()
            snapshot = service.get_market_snapshot(now=now)
            exclusions = tuple(service.get_market_exclusions(now=now))
            if not snapshot.terms and not exclusions:
                return Result.empty(NO_DATA_MESSAGE)
            health = assess_system_health(service, now=now)
            state = service.get_market_state(now=now)
            today_start = now.astimezone(_nz()).replace(hour=0, minute=0, second=0, microsecond=0)
            today = service.get_changes(
                days=None, since=today_start.astimezone(timezone.utc), now=now, scope_to_market=True
            )
            week = service.get_changes(days=7, now=now, scope_to_market=True)
            moves = [c for c in week if c.is_rate_move and not c.suspicious]
            by_term: dict[int, int] = {}
            for change in moves:
                by_term[change.term_months] = by_term.get(change.term_months, 0) + 1
            terms = tuple(
                sorted(set(snapshot.term_months) | {e.term_months for e in exclusions})
            )
            banks = tuple(service.config.selected_banks)
            cells: dict[tuple[str, int], MatrixCell] = {}
            for term in snapshot.term_months:
                market = snapshot[term]
                for entry in market.entries:
                    cells[(entry.bank, term)] = MatrixCell(
                        rate_pct=entry.rate_pct,
                        is_leader=entry.rank == 1,
                        rate_category=entry.rate_category,
                    )
            for exclusion in exclusions:
                cells.setdefault(
                    (exclusion.bank, exclusion.term_months),
                    MatrixCell(rate_pct=exclusion.rate_pct, stale=True),
                )
            default_term = 12 if 12 in terms else (terms[0] if terms else None)
            run = health.latest_run
            return Result.ok(
                Overview(
                    header=HeaderInfo(
                        last_update=run.completed_at if run else None,
                        market_state=state.state.value,
                        fresh_banks=health.fresh_bank_count,
                        configured_banks=len(health.banks),
                    ),
                    changes_today=len(today),
                    changes_7d=len(week),
                    banks_repricing_7d=state.activity.banks_changed,
                    dominant_direction=state.dominant_direction,
                    most_active_term=(
                        max(by_term, key=lambda t: (by_term[t], -t)) if by_term else None
                    ),
                    market_state_rationale=state.rationale,
                    matrix=MarketMatrix(banks=banks, terms=terms, cells=cells),
                    default_term=default_term,
                    exclusions=exclusions,
                )
            )

        return self._guard(build)

    def term_summary(self, term_months: int) -> Result[TermSummary]:
        def build(service: PricingIntelligenceService) -> Result[TermSummary]:
            now = self._clock()
            market = service.get_market_snapshot(now=now).term(term_months)
            if market is None:
                return Result.empty(f"No comparable market data at {term_months} months.")
            health = assess_system_health(service, now=now)
            return Result.ok(
                TermSummary(
                    term_months=term_months,
                    leaders=market.market_leaders,
                    lowest_pct=market.lowest_rate,
                    median_pct=market.median_rate,
                    highest_pct=market.highest_rate,
                    spread_bps=market.spread_bps,
                    banks_observed=market.bank_count,
                    fresh_banks=health.fresh_bank_count,
                    ranking=tuple(
                        RankedBank(e.rank, e.bank, e.rate_pct, e.product_name)
                        for e in market.ranking()
                    ),
                )
            )

        return self._guard(build)

    def bank_positions(self, term_months: int) -> Result[tuple[BankPosition, ...]]:
        def build(service: PricingIntelligenceService) -> Result[tuple[BankPosition, ...]]:
            now = self._clock()
            market = service.get_market_snapshot(now=now).term(term_months)
            if market is None:
                return Result.empty(f"No comparable market data at {term_months} months.")
            health = {b.bank: b for b in assess_system_health(service, now=now).banks}
            last_change: dict[str, datetime] = {}
            for change in service.get_changes(
                days=None, term_months=term_months, now=now, scope_to_market=True
            ):
                if change.is_rate_move and not change.suspicious:
                    last_change.setdefault(change.bank, change.detected_at)  # newest first
            return Result.ok(
                tuple(
                    BankPosition(
                        bank=e.bank,
                        rate_pct=e.rate_pct,
                        rank=e.rank,
                        vs_leader_bps=rate_change_bps(market.best_rate, e.rate_pct),
                        vs_median_bps=rate_change_bps(market.median_rate, e.rate_pct),
                        last_change_at=last_change.get(e.bank),
                        freshness=health[e.bank].freshness.state
                        if e.bank in health
                        else FreshnessState.UNKNOWN,
                    )
                    for e in market.ranking()
                )
            )

        return self._guard(build)


def _nz():
    from balance_pricing_engine.dashboard.formatting import NZ_TZ

    return NZ_TZ
```

Note `ProductType`/`timedelta` imports may be unused here — remove any ruff flags. Run `python3 -m pytest tests/unit/test_dashboard_services_market.py -v`. Expected PASS. If `changes_7d >= 1` fails because the fixture's run-2 `detected_at` (NOW-1d) is outside `scope_to_market` terms, check `config.tracked_terms_months` contains 12 (default list); if `get_changes(days=None, since=…)` conflicts, use `days=None` with `since` only per its docstring (since wins).

- [ ] **Step 7: Lint/format/type on new files**

Run: `python3 -m ruff check src/balance_pricing_engine/dashboard tests/unit/test_dashboard_*.py && python3 -m ruff format --check src/balance_pricing_engine/dashboard/{settings,results,formatting,models,services}.py tests/unit/test_dashboard_settings.py tests/unit/test_dashboard_formatting.py tests/unit/test_dashboard_services_market.py && python3 -m mypy --ignore-missing-imports src/balance_pricing_engine/dashboard/{settings,results,formatting,models,services}.py`
Expected: clean (fix any new-file findings; do not touch pre-existing files' formatting).

- [ ] **Step 8: Commit**

```bash
git add src/balance_pricing_engine/dashboard src/balance_pricing_engine/pricing_intelligence/snapshot.py tests/unit
git commit -m "feat: dashboard settings, formatting and market read services" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

**Acceptance:** every displayed market number equals the backend call; missing/corrupt/empty DBs yield EMPTY/ERROR results; no file is created by a read.

---

### Task 3: Router, shared page plumbing, Overview page, removal of the old script

**Files:**
- Create: `dashboard/charts.py` (dot-plot only for now), `dashboard/pages/__init__.py`, `pages/_common.py`, `pages/overview.py`, `pages/scenario_lab.py` (moved code, Task 7 tests it), `tests/unit/dashboard_harness.py`
- Replace: `dashboard/app.py`
- Delete: `tests/unit/test_dashboard.py`, `test_dashboard_runtime.py`, `test_dashboard_market_intelligence.py` (after porting per the migration map)
- Test: `tests/unit/test_dashboard_pages_overview.py`, `tests/unit/test_dashboard_app_smoke.py`

**Interfaces:**
- Consumes (Task 2): `DashboardServices`, `Result`, `Overview`, formatting helpers, `load_settings`.
- Produces:
  - `pages/_common.py`: `cached(services: DashboardServices, method: str, *args) -> Result` (60 s `st.cache_data` keyed on `services.db_stamp()`, args, never used for health), `unwrap(result: Result[T], *, empty_as_info: bool = True) -> Optional[T]` (renders `st.info`/`st.error`, returns value or `None`), `render_header(services)`, `matrix_frame(matrix: MarketMatrix) -> pandas.DataFrame`
  - `pages/overview.py`: `render(services: DashboardServices) -> None`
  - `app.py`: `main()` router; module-level call guarded so import is side-effect free.
  - `tests/unit/dashboard_harness.py`: `run_page(page: str, database: Path, now: datetime, *, setup: str = "") -> AppTest`

- [ ] **Step 1: Spike — does `AppTest` run `st.navigation` apps?** Write throwaway `test_dashboard_app_smoke.py`:

```python
from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
ROOT = Path(__file__).resolve().parents[2]


def test_router_renders_overview_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICING_INTELLIGENCE_DB_PATH", str(tmp_path / "none.db"))
    app = AppTest.from_file(
        str(ROOT / "src/balance_pricing_engine/dashboard/app.py"), default_timeout=30
    ).run()
    assert not app.exception
    assert any("NZ Retail Bank Pricing Watch" in t.value for t in app.title)
```

Implement the router first (Step 4); if `AppTest` cannot execute `st.navigation` (exception mentioning navigation/pages), switch the router to a sidebar `st.radio("Page", names, key="page")` that calls `PAGES[name](services)` — page modules are identical either way. Record the outcome in the commit message.

- [ ] **Step 2: Harness** — `tests/unit/dashboard_harness.py`:

```python
"""Run one dashboard page against a fixture database with AppTest."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest


def run_page(page: str, database: Path, now: datetime, *, setup: str = "") -> "AppTest":
    script = f"""
from datetime import datetime
{setup}
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.dashboard.pages import {page} as page

settings = load_settings({{}}, database_path={str(database)!r})
services = DashboardServices(settings, clock=lambda: datetime.fromisoformat({now.isoformat()!r}))
page.render(services)
"""
    return AppTest.from_string(script, default_timeout=30).run()
```

- [ ] **Step 3: Failing Overview tests** — `tests/unit/test_dashboard_pages_overview.py`:

`tests/unit/` has no `__init__.py`, so pytest's default `prepend` import mode puts that directory on `sys.path`: sibling helpers import by bare module name. Move `make_services` (from Task 2's `test_dashboard_services_market.py`) into `dashboard_harness.py` in this task and import it from there in every later test file instead of importing from another test module.

```python
from dashboard_harness import run_page


def test_overview_shows_matrix_summary_and_ranking_with_ties(market_factory):
    market = market_factory()
    app = run_page("overview", market.database, market.now)
    assert not app.exception
    matrix = app.dataframe[0].value
    assert matrix.loc["ANZ", "1Y"] == "◆ 4.79%"
    assert matrix.loc["ASB", "1Y"] == "4.89%"
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Lowest"] == "4.79%" and metrics["Median"] == "4.85%"
    assert metrics["Highest"] == "4.89%" and metrics["Spread"] == "10 bps"
    ranking = app.dataframe[1].value
    assert list(ranking["Rank"]) == [1, 2, 2, 4, 4]


def test_overview_marks_stale_bank_and_explains_exclusion(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    app = run_page("overview", market.database, market.now)
    assert app.dataframe[0].value.loc["Westpac", "1Y"] == "⚠ 4.85% (stale)"
    assert any("Westpac excluded" in w.value and "168" in w.value for w in app.warning)


def test_overview_without_data_shows_guidance_not_raw_values(tmp_path):
    from datetime import datetime, timezone
    app = run_page("overview", tmp_path / "none.db", datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not app.exception
    assert any("No market data" in i.value for i in app.info)


def test_term_selector_changes_the_summary(market_factory):
    market = market_factory()
    app = run_page("overview", market.database, market.now)
    app.selectbox[0].set_value(60).run()
    assert not app.exception
```

(The last asserts a term with no data degrades via EMPTY info, not an exception; 60 may not exist — selectbox options come from `matrix.terms`, so use the first option different from 12 if the fixture only has 12: replace with a test that `selectbox[0].options == ["1Y"]`. Adjust to the fixture; do not leave a failing assumption.)

Run → FAIL.

- [ ] **Step 4: Implement shared plumbing and Overview**

`pages/_common.py`:

```python
"""Plumbing shared by pages: cached reads, result rendering, header."""

from __future__ import annotations

from typing import Optional, TypeVar

import pandas as pd
import streamlit as st

from balance_pricing_engine.dashboard.formatting import EM_DASH, fmt_nz_datetime, fmt_rate_pct, fmt_term
from balance_pricing_engine.dashboard.models import MarketMatrix
from balance_pricing_engine.dashboard.results import Result
from balance_pricing_engine.dashboard.services import DashboardServices

T = TypeVar("T")
CACHE_TTL_SECONDS = 60


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _cached(_services: DashboardServices, method: str, db_stamp: tuple, args: tuple) -> Result:
    return getattr(_services, method)(*args)


def cached(services: DashboardServices, method: str, *args) -> Result:
    """Cached *stored-state* read. Never use for health/freshness (they depend on 'now')."""
    return _cached(services, method, services.db_stamp(), args)


def unwrap(result: Result[T]) -> Optional[T]:
    if result.status == "ERROR":
        st.error(result.message)
        return None
    if result.status == "EMPTY":
        st.info(result.message)
        return None
    return result.value


def matrix_frame(matrix: MarketMatrix) -> pd.DataFrame:
    rows = {}
    for bank in matrix.banks:
        row = {}
        for term in matrix.terms:
            cell = matrix.cells.get((bank, term))
            if cell is None or cell.rate_pct is None:
                text = EM_DASH
            elif cell.stale:
                text = f"⚠ {fmt_rate_pct(cell.rate_pct)} (stale)"
            elif cell.is_leader:
                text = f"◆ {fmt_rate_pct(cell.rate_pct)}"
            else:
                text = fmt_rate_pct(cell.rate_pct)
            row[fmt_term(term)] = text
        rows[bank] = row
    frame = pd.DataFrame.from_dict(rows, orient="index")
    frame.index.name = "Bank"
    return frame


def render_header(services: DashboardServices) -> None:
    st.title("NZ Retail Bank Pricing Watch")
    result = services.header()  # uncached: freshness depends on 'now'
    header = result.value
    if header is None:
        st.caption(result.message)
        return
    cols = st.columns(3)
    cols[0].caption(f"Last successful update: {fmt_nz_datetime(header.last_update)} (Pacific/Auckland)")
    cols[1].caption(f"Market activity: {header.market_state or EM_DASH}")
    cols[2].caption(f"Collector health: {header.fresh_banks} / {header.configured_banks} fresh")
```

`pages/overview.py`:

```python
"""Overview: what is the market doing right now?"""

from __future__ import annotations

import streamlit as st

from balance_pricing_engine.dashboard.formatting import EM_DASH, fmt_bps, fmt_nz_datetime, fmt_rate_pct, fmt_term
from balance_pricing_engine.dashboard.pages._common import cached, matrix_frame, unwrap
from balance_pricing_engine.dashboard.services import DashboardServices
import pandas as pd


def render(services: DashboardServices) -> None:
    overview = unwrap(cached(services, "overview"))
    if overview is None:
        return

    cols = st.columns(5)
    cols[0].metric("Changes today", overview.changes_today)
    cols[1].metric("Changes (7 days)", overview.changes_7d)
    cols[2].metric("Banks repricing", overview.banks_repricing_7d)
    cols[3].metric("Dominant direction", overview.dominant_direction.title())
    cols[4].metric("Most active term", fmt_term(overview.most_active_term) if overview.most_active_term else EM_DASH)
    st.caption(overview.market_state_rationale)

    st.subheader("Mortgage market (advertised specials)")
    st.dataframe(matrix_frame(overview.matrix), width="stretch")
    st.caption("◆ term leader · ⚠ stale (excluded from rankings and statistics) · — not available")
    for exclusion in overview.exclusions:
        st.warning(
            f"{exclusion.bank} excluded at {fmt_term(exclusion.term_months)}: latest validated "
            f"observation is {exclusion.age_hours:.0f} h old "
            f"(limit {exclusion.threshold_hours:.0f} h)."
        )

    terms = [t for t in overview.matrix.terms]
    if not terms:
        return
    labels = [fmt_term(t) for t in terms]
    default = terms.index(overview.default_term) if overview.default_term in terms else 0
    chosen = st.selectbox("Term", labels, index=default, key="overview_term")
    term = terms[labels.index(chosen)]
    summary = unwrap(cached(services, "term_summary", term))
    if summary is None:
        return
    top = st.columns(4)
    top[0].metric("Market leader", ", ".join(summary.leaders))
    top[1].metric("Lowest", fmt_rate_pct(summary.lowest_pct))
    top[2].metric("Median", fmt_rate_pct(summary.median_pct))
    top[3].metric("Highest", fmt_rate_pct(summary.highest_pct))
    bottom = st.columns(3)
    bottom[0].metric("Spread", f"{summary.spread_bps:.0f} bps")
    bottom[1].metric("Banks observed", summary.banks_observed)
    bottom[2].metric("Fresh banks", summary.fresh_banks)
    st.dataframe(
        pd.DataFrame(
            [{"Rank": r.rank, "Bank": r.bank, "Rate": fmt_rate_pct(r.rate_pct), "Product": r.product_name}
             for r in summary.ranking]
        ),
        hide_index=True, width="stretch",
    )
```

(metrics label for `Spread`: test asserts `"10 bps"`; `fmt_bps` shows signed; use `f"{spread:.0f} bps"` as written. The test's `metrics["Lowest"]` etc. match labels above; `Market leader` metric value for a tie lists all leaders.)

`pages/scenario_lab.py` — move the old `_run`/tabs code from `app.py` lines 247-314 into `def render(services) -> None` (sidebar YAML text area stays in the page body as an expander; keep behaviour: config YAML text area default `services.settings.engine_config_text`, button "Run scenario", `include_sensitivity` checkbox; `@st.cache_data` `_run`). Preserve the exact error handling (`TypeError, ValidationError, yaml.YAMLError` → `st.error("Config error: …")`; `NotImplementedError` → `st.error("Scenario option error: …")`).

`app.py`:

```python
"""Pricing Watch dashboard entry point.

Launch: ``PYTHONPATH=src streamlit run src/balance_pricing_engine/dashboard/app.py``
(``pip install -e ".[dashboard]"`` makes the PYTHONPATH prefix unnecessary).
"""

from __future__ import annotations

import streamlit as st

from balance_pricing_engine.dashboard.pages import (
    market_watch, operations, overview, rate_changes, scenarios, system_health, trends,
)
from balance_pricing_engine.dashboard.pages._common import render_header
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings


@st.cache_resource
def _settings():
    return load_settings()


def main() -> None:
    st.set_page_config(page_title="NZ Pricing Watch", layout="wide")
    services = DashboardServices(_settings())

    def page(module, title, path, default=False):
        return st.Page(lambda: module.render(services), title=title, url_path=path, default=default)

    navigation = st.navigation(
        [
            page(overview, "Overview", "overview", True),
            page(market_watch, "Market Watch", "market-watch"),
            page(rate_changes, "Rate Changes", "rate-changes"),
            page(trends, "Historical Trends", "trends"),
            page(scenarios, "Pricing Scenarios", "scenarios"),
            page(operations, "Operations", "operations"),
            page(system_health, "System Health", "system-health"),
        ]
    )
    render_header(services)
    navigation.run()


main()
```

Pages `market_watch`, `rate_changes`, `trends`, `scenarios`, `operations`, `system_health` do not exist yet: create each as a stub module in this task containing only `def render(services): st.info("Coming in a later task.")`?? — **not allowed as a placeholder in the final tree**, but they are replaced in Tasks 4–9 of this same branch; keep stubs minimal, and Task 10's final grep (`grep -rn "Coming in a later task" src`) must return nothing. Note: `st.Page(lambda …)` requires distinct callables with distinct `url_path`s — `url_path` is given, so fine.

- [ ] **Step 5: Run tests; port + delete old tests**

Run: `python3 -m pytest tests/unit/test_dashboard_pages_overview.py tests/unit/test_dashboard_app_smoke.py -v` → PASS. Then walk the migration map: the old files' assertions are replaced by tests in this and later tasks. `git rm tests/unit/test_dashboard.py tests/unit/test_dashboard_runtime.py tests/unit/test_dashboard_market_intelligence.py`. (Scenario Lab, history, health and TD-section successors land in Tasks 6/7/9; until then the branch intentionally has less dashboard coverage — Task 10 re-verifies the map line by line.)

Run: `python3 -m pytest -q -m 'not live_rates'` → Expected: 1603 − (deleted old tests) + new tests, 0 failures. Lint/format/mypy as in Task 2 Step 7 for the new files.

- [ ] **Step 6: Commit**

```bash
git add -A src/balance_pricing_engine/dashboard tests/unit
git commit -m "feat: dashboard router, shared page plumbing and Overview page" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

**Acceptance:** `streamlit run …/dashboard/app.py` lands on Overview; matrix/leader/median/spread/ranking equal backend values; stale banks are marked and explained; empty/corrupt DB renders guidance.

---

### Task 4: Market Watch page

**Files:**
- Modify: `dashboard/charts.py` (add `position_dot_plot`), replace stub `pages/market_watch.py`
- Test: `tests/unit/test_dashboard_charts.py`, `tests/unit/test_dashboard_pages_market_watch.py`

**Interfaces:**
- Consumes: `DashboardServices.bank_positions(term) -> Result[tuple[BankPosition,...]]`, `term_summary`, `overview` (for term/bank choices), `fmt_*`.
- Produces: `charts.position_dot_plot(positions: Sequence[BankPosition], median_pct: float, selected_bank: Optional[str]) -> alt.Chart`; `pages/market_watch.py::render(services)`.

- [ ] **Step 1: Failing chart test** — `tests/unit/test_dashboard_charts.py`:

```python
from balance_pricing_engine.dashboard.charts import position_dot_plot
from balance_pricing_engine.dashboard.models import BankPosition
from balance_pricing_engine.pricing_intelligence.health import FreshnessState


def _pos(bank, rate, rank):
    return BankPosition(bank, rate, rank, 0.0, 0.0, None, FreshnessState.FRESH)


def test_dot_plot_has_a_point_per_bank_a_median_rule_and_flags_leader_and_selection():
    spec = position_dot_plot(
        [_pos("ANZ", 4.79, 1), _pos("ASB", 4.89, 4)], median_pct=4.85, selected_bank="ASB"
    ).to_dict()
    points = [v for ds in spec["datasets"].values() for v in ds]
    roles = {p["bank"]: p["role"] for p in points if "bank" in p}
    assert roles == {"ANZ": "Leader", "ASB": "Selected"}
    rules = [p for p in points if p.get("rate") == 4.85 and "bank" not in p]
    assert rules, "median rule missing"
```

Run → FAIL. Implement `charts.py`:

```python
"""Altair chart specs. Inputs are view-model data only; no market maths here."""

from __future__ import annotations

from typing import Optional, Sequence

import altair as alt
import pandas as pd

from balance_pricing_engine.dashboard.models import BankPosition

ROLE_COLOURS = {"Leader": "#1b6ca8", "Selected": "#c0392b", "Other": "#7f8c8d"}


def position_dot_plot(
    positions: Sequence[BankPosition], median_pct: float, selected_bank: Optional[str] = None
) -> alt.Chart:
    """Where each bank sits on one term, with market median and leader/selected emphasised."""
    frame = pd.DataFrame(
        [
            {
                "bank": p.bank,
                "rate": p.rate_pct,
                "role": "Selected" if p.bank == selected_bank else ("Leader" if p.rank == 1 else "Other"),
            }
            for p in positions
        ]
    )
    base = alt.Chart(frame).encode(
        x=alt.X("rate:Q", title="Advertised rate (%)", scale=alt.Scale(zero=False)),
        y=alt.Y("bank:N", sort="x", title=None),
    )
    dots = base.mark_circle(size=140).encode(
        color=alt.Color(
            "role:N",
            scale=alt.Scale(domain=list(ROLE_COLOURS), range=list(ROLE_COLOURS.values())),
            legend=alt.Legend(title=None),
        ),
        tooltip=["bank", alt.Tooltip("rate:Q", format=".2f"), "role"],
    )
    median = (
        alt.Chart(pd.DataFrame({"rate": [median_pct]}))
        .mark_rule(strokeDash=[4, 4])
        .encode(x="rate:Q")
    )
    return (median + dots).properties(height=40 * max(len(frame), 3))
```

Run → PASS.

- [ ] **Step 2: Failing page test** — `tests/unit/test_dashboard_pages_market_watch.py`:

```python
from dashboard_harness import run_page  # same import style as Task 3


def test_market_watch_shows_rank_gaps_and_freshness(market_factory):
    market = market_factory()
    app = run_page("market_watch", market.database, market.now)
    assert not app.exception
    frame = app.dataframe[0].value.set_index("Bank")
    assert frame.loc["ASB", "Rank"] == 4
    assert frame.loc["ASB", "vs leader"] == "+10 bps"
    assert frame.loc["ASB", "vs median"] == "+4 bps"
    assert frame.loc["ANZ", "Freshness"] == "FRESH"


def test_market_watch_selected_bank_card(market_factory):
    market = market_factory()
    app = run_page("market_watch", market.database, market.now)
    app.selectbox(key="market_watch_bank").set_value("ASB").run()
    assert not app.exception
    values = {m.label: m.value for m in app.metric}
    assert values["Current"] == "4.89%" and values["Rank"] == "#4"
    assert values["vs leader"] == "+10 bps" and values["vs median"] == "+4 bps"
```

Run → FAIL. Implement `pages/market_watch.py`:

```python
"""Market Watch: where does each bank sit competitively?"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from balance_pricing_engine.dashboard.charts import position_dot_plot
from balance_pricing_engine.dashboard.formatting import (
    fmt_bps, fmt_freshness, fmt_nz_datetime, fmt_rate_pct, fmt_term,
)
from balance_pricing_engine.dashboard.pages._common import cached, unwrap
from balance_pricing_engine.dashboard.services import DashboardServices


def render(services: DashboardServices) -> None:
    overview = unwrap(cached(services, "overview"))
    if overview is None or not overview.matrix.terms:
        return
    terms = list(overview.matrix.terms)
    labels = [fmt_term(t) for t in terms]
    left, right = st.columns(2)
    chosen = left.selectbox(
        "Term", labels, index=terms.index(overview.default_term) if overview.default_term in terms else 0,
        key="market_watch_term",
    )
    term = terms[labels.index(chosen)]
    positions = unwrap(cached(services, "bank_positions", term))
    summary = unwrap(cached(services, "term_summary", term))
    if positions is None or summary is None:
        return
    banks = [p.bank for p in positions]
    bank = right.selectbox("Highlight bank", banks, key="market_watch_bank")

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Bank": p.bank, "Rank": p.rank, "Rate": fmt_rate_pct(p.rate_pct),
                    "vs leader": fmt_bps(p.vs_leader_bps), "vs median": fmt_bps(p.vs_median_bps),
                    "Last change": fmt_nz_datetime(p.last_change_at) if p.last_change_at else "No recent change",
                    "Freshness": fmt_freshness(p.freshness),
                }
                for p in positions
            ]
        ),
        hide_index=True, width="stretch",
    )

    selected = next(p for p in positions if p.bank == bank)
    cols = st.columns(5)
    cols[0].metric("Current", fmt_rate_pct(selected.rate_pct))
    cols[1].metric("Rank", f"#{selected.rank}")
    cols[2].metric("vs leader", fmt_bps(selected.vs_leader_bps))
    cols[3].metric("vs median", fmt_bps(selected.vs_median_bps))
    cols[4].metric("Freshness", fmt_freshness(selected.freshness))

    st.altair_chart(position_dot_plot(positions, summary.median_pct, bank), width="stretch")
    st.caption("Dashed line: market median. Blue: leader. Red: highlighted bank.")
```

(The page spec also lists product and rate-type filters: the backend's comparable category is fixed per product (`special` for mortgages) so no rate-type control is offered — document in the page caption "Comparable tier: advertised specials".) Run → PASS.

- [ ] **Step 3: Lint/format/mypy new files; commit**

```bash
git add src/balance_pricing_engine/dashboard tests/unit
git commit -m "feat: Market Watch page with competitive-position dot plot" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

**Acceptance:** rank/vs-leader/vs-median/freshness on screen equal `bank_positions`; chart marks leader, median, selected bank.

---

### Task 5: Rate Changes page (+ event context)

**Files:**
- Modify: `dashboard/models.py` (add `ChangeWindow`, `ChangeFilters`, `ChangeRow`, `EventDetail`), `dashboard/services.py` (add `changes`, `event_detail`), replace stub `pages/rate_changes.py`
- Test: `tests/unit/test_dashboard_services_changes.py`, `tests/unit/test_dashboard_pages_rate_changes.py`

**Interfaces:**
- Consumes: `service.get_changes`, `service.get_market_snapshot(as_of_run_id=…)`, `store.get_previous_run(before_run_id=…, product_type=…)`, `service.get_market_state(now=…)`, `service.get_rate_history(...)`.
- Produces:
  - `models.ChangeWindow(kind: Literal["today","7d","30d","custom"], start: Optional[date]=None, end: Optional[date]=None)`
  - `models.ChangeFilters(bank: Optional[str]=None, term_months: Optional[int]=None, direction: Optional[str]=None, change_type: Optional[str]=None)`
  - `models.ChangeRow(key: str, detected_at, bank, term_months: Optional[int], previous_pct: Optional[float], current_pct: Optional[float], change_bps: Optional[float], direction: str, change_type: str, previous_rank: Optional[int], new_rank: Optional[int])` where `key = f"{observation_key}|{change_type}|{detected_at.isoformat()}|{run_id}"`
  - `models.EventDetail(row: ChangeRow, product_name: str, rate_category: str, source_url: Optional[str], observed_at: Optional[datetime], effective_date_text: Optional[str], parser_version: Optional[str], market_median_pct: Optional[float], market_state: Optional[str])`
  - `DashboardServices.changes(window: ChangeWindow, filters: ChangeFilters = ChangeFilters()) -> Result[tuple[ChangeRow,...]]`, `.event_detail(key: str) -> Result[EventDetail]`

- [ ] **Step 1: Failing service tests** (flow **B** is here) — `tests/unit/test_dashboard_services_changes.py`:

```python
from balance_pricing_engine.dashboard.models import ChangeFilters, ChangeWindow
from test_dashboard_services_market import make_services  # or move make_services into conftest as a fixture


def test_anz_move_has_exact_bps_and_ranks(market_factory):
    market = market_factory()
    rows = make_services(market).changes(ChangeWindow("7d")).value
    anz = next(r for r in rows if r.bank == "ANZ")
    assert (anz.previous_pct, anz.current_pct, anz.change_bps) == (4.89, 4.79, -10.0)
    assert anz.direction == "DOWN" and anz.term_months == 12
    assert anz.previous_rank == 3  # 4.89 tied with ASB/Kiwibank behind the 4.85 pair
    assert anz.new_rank == 1


def test_filters_narrow_the_rows(market_factory):
    market = market_factory()
    services = make_services(market)
    assert services.changes(ChangeWindow("7d"), ChangeFilters(bank="BNZ")).status == "EMPTY"
    assert services.changes(ChangeWindow("7d"), ChangeFilters(direction="UP")).status == "EMPTY"
    assert services.changes(ChangeWindow("7d"), ChangeFilters(term_months=12)).is_ok


def test_today_window_excludes_yesterdays_move(market_factory):
    market = market_factory()
    assert make_services(market).changes(ChangeWindow("today")).status == "EMPTY"


def test_event_detail_exposes_provenance_and_market_context(market_factory):
    market = market_factory()
    services = make_services(market)
    row = services.changes(ChangeWindow("7d")).value[0]
    detail = services.event_detail(row.key).value
    assert detail.source_url.endswith("/anz/rates")
    assert detail.parser_version and detail.rate_category == "special"
    assert detail.market_median_pct == 4.85
    assert detail.market_state in {"LOW", "NORMAL", "ELEVATED", "HIGH"}
```

Run → FAIL. Note: ANZ's previous rank — in run-1 the market is BNZ 4.85, Westpac 4.85 (rank 1), then ANZ/ASB/Kiwibank 4.89 (rank 3). Median at event run-2: 4.85 (4.79, 4.85, 4.85, 4.89, 4.89 → median 4.85). If `as_of_run_id` semantics differ from "state at end of that run" (see spec §3 risk), the rank assertions will show it: debug with `systematic-debugging` (inspect `get_market_snapshot(as_of_run_id="run-2")` directly) and fix the derivation — not the assertion.

- [ ] **Step 2: Implement services additions** — add the models, then in `services.py`:

```python
    # -------------------------------------------------------------- changes

    def _window_args(self, window: ChangeWindow, now: datetime) -> dict:
        if window.kind == "today":
            midnight = now.astimezone(NZ_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            return {"days": None, "since": midnight.astimezone(timezone.utc)}
        if window.kind == "30d":
            return {"days": 30}
        if window.kind == "custom" and window.start is not None:
            start = datetime.combine(window.start, datetime.min.time(), tzinfo=NZ_TZ)
            return {"days": None, "since": start.astimezone(timezone.utc)}
        return {"days": 7}

    def changes(
        self, window: ChangeWindow, filters: Optional[ChangeFilters] = None
    ) -> Result[tuple[ChangeRow, ...]]:
        filters = filters or ChangeFilters()

        def build(service: PricingIntelligenceService) -> Result[tuple[ChangeRow, ...]]:
            now = self._clock()
            changes = service.get_changes(
                now=now, scope_to_market=True, bank=filters.bank,
                term_months=filters.term_months, **self._window_args(window, now),
            )
            if window.kind == "custom" and window.end is not None:
                end = datetime.combine(window.end, datetime.max.time(), tzinfo=NZ_TZ)
                changes = [c for c in changes if c.detected_at <= end]
            changes = [c for c in changes if not c.suspicious]  # shown in System Health instead
            if filters.direction:
                changes = [c for c in changes if c.direction == filters.direction]
            if filters.change_type:
                changes = [c for c in changes if c.change_type.value == filters.change_type]
            if not changes:
                return Result.empty("No rate changes in this window.")
            ranks = _RankCache(service)
            return Result.ok(tuple(self._row(c, ranks) for c in changes))

        return self._guard(build)

    @staticmethod
    def _row(change, ranks: "_RankCache") -> ChangeRow:
        previous_rank, new_rank = ranks.for_change(change)
        return ChangeRow(
            key=_event_key(change), detected_at=change.detected_at, bank=change.bank,
            term_months=change.term_months, previous_pct=change.previous_rate_pct,
            current_pct=change.current_rate_pct, change_bps=change.change_bps,
            direction=change.direction, change_type=change.change_type.value,
            previous_rank=previous_rank, new_rank=new_rank,
        )

    def event_detail(self, key: str) -> Result[EventDetail]:
        def build(service: PricingIntelligenceService) -> Result[EventDetail]:
            now = self._clock()
            change = next(
                (c for c in service.get_changes(days=None, now=now, scope_to_market=True)
                 if _event_key(c) == key),
                None,
            )
            if change is None:
                return Result.empty("That event is no longer in the stored history.")
            ranks = _RankCache(service)
            row = self._row(change, ranks)
            history = service.get_rate_history(
                bank=change.bank, term_months=change.term_months,
                rate_category=change.rate_category, days=None, now=change.detected_at,
            )
            record = next(
                (r for r in reversed(history)
                 if r.product_name == change.product_name and r.captured_at_utc <= change.detected_at),
                None,
            )
            market = ranks.new_market(change)
            state = service.get_market_state(now=change.detected_at)
            return Result.ok(
                EventDetail(
                    row=row, product_name=change.product_name, rate_category=change.rate_category,
                    source_url=change.source_url or (record.source_url if record else None),
                    observed_at=record.captured_at_utc if record else None,
                    effective_date_text=record.effective_date_text if record else None,
                    parser_version=record.parser_version if record else None,
                    market_median_pct=market.median_rate if market else None,
                    market_state=state.state.value,
                )
            )

        return self._guard(build)
```

with module-level helpers:

```python
def _event_key(change) -> str:
    return f"{change.observation_key}|{change.change_type.value}|{change.detected_at.isoformat()}|{change.run_id}"


class _RankCache:
    """Resolve market rank before/after an event, one snapshot per distinct run."""

    def __init__(self, service: PricingIntelligenceService) -> None:
        self._service = service
        self._snapshots: dict[Optional[str], Optional[MarketSnapshot]] = {}

    def _snapshot(self, run_id: Optional[str]) -> Optional[MarketSnapshot]:
        if run_id is None:
            return None
        if run_id not in self._snapshots:
            self._snapshots[run_id] = self._service.get_market_snapshot(as_of_run_id=run_id)
        return self._snapshots[run_id]

    def _previous_run_id(self, run_id: str) -> Optional[str]:
        previous = self._service.store.get_previous_run(
            before_run_id=run_id, product_type=ProductType.HOME_LOAN
        )
        return previous.run_id if previous else None

    def new_market(self, change):
        snapshot = self._snapshot(change.run_id)
        return snapshot.term(change.term_months) if snapshot and change.term_months else None

    def for_change(self, change) -> tuple[Optional[int], Optional[int]]:
        if not change.is_rate_move or change.run_id is None or change.term_months is None:
            return (None, None)
        new_market = self.new_market(change)
        previous_run = self._previous_run_id(change.run_id)
        previous_snapshot = self._snapshot(previous_run)
        previous_market = previous_snapshot.term(change.term_months) if previous_snapshot else None
        return (
            previous_market.rank_for(change.bank) if previous_market else None,
            new_market.rank_for(change.bank) if new_market else None,
        )
```

(import `ChangeRow`, `ChangeFilters`, `ChangeWindow`, `EventDetail`, `MarketSnapshot`, `ProductType`, `NZ_TZ`.) Run the service tests → PASS.

- [ ] **Step 3: Page test + implementation** — `tests/unit/test_dashboard_pages_rate_changes.py`:

```python
def test_rate_changes_defaults_to_7_days_and_lists_the_move(market_factory):
    market = market_factory()
    app = run_page("rate_changes", market.database, market.now)
    assert not app.exception
    row = app.dataframe[0].value.iloc[0]
    assert row["Bank"] == "ANZ" and row["Old"] == "4.89%" and row["New"] == "4.79%"
    assert row["Movement"] == "-10 bps" and row["Prev rank"] == 3 and row["New rank"] == 1


def test_rate_changes_empty_state_is_a_sentence(market_factory):
    market = market_factory(anz_new_rate=4.89)  # no movement
    app = run_page("rate_changes", market.database, market.now)
    assert any("No rate changes" in i.value for i in app.info)


def test_selecting_an_event_shows_provenance_with_a_link(market_factory):
    market = market_factory()
    app = run_page("rate_changes", market.database, market.now)
    app.selectbox(key="rate_changes_event").select_index(0).run()
    assert not app.exception
    text = " ".join(m.value for m in app.markdown)
    assert "Parser version" in text and "/anz/rates" in text
```

Implement `pages/rate_changes.py`: window radio (`Today`, `7 days` default, `30 days`, `Custom` → two `st.date_input`s), filter selects (bank from `services.settings.intelligence.selected_banks`, term from overview terms, direction `UP/DOWN`, type from `ChangeType` values via the rows present), results via `unwrap(cached(services, "changes", window, filters))` (frozen dataclasses are hashable; `ChangeWindow`/`ChangeFilters` must be `@dataclass(frozen=True)`), dataframe columns `Time, Bank, Term, Old, New, Movement, Prev rank, New rank, Type` (`fmt_nz_datetime`, `fmt_rate_pct`, `fmt_bps`, `fmt_term`; missing rank → `—`), then `st.selectbox("Event", range(len(rows)), format_func=…, key="rate_changes_event")` → `event_detail(rows[i].key)` rendered with `st.markdown` lines: `**Source:** [official page](url)` (only if `source_url` startswith `http`), Observed, Effective date (`effective_date_text or "not published"`), Rate type, Previous/new rank, Market median at event, Market state, `Parser version`. Never render raw HTML.

Run all three test files → PASS. Lint/format/mypy; commit:

```bash
git commit -am "feat: Rate Changes page with filters, rank context and provenance" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

**Acceptance:** ANZ 4.89→4.79 shows −10 bps, rank 3→1, median 4.85; filters work; source link only for http(s) URLs.

---

### Task 6: Historical Trends page

**Files:**
- Modify: `dashboard/models.py` (`HistoryRange`, `HistoryData`, `HistoryPoint`, `MarketPoint`, `TimelineEvent`), `dashboard/services.py` (`history`), `dashboard/charts.py` (`history_lines`, `market_trend`, `repricing_timeline`), replace stub `pages/trends.py`
- Test: `tests/unit/test_dashboard_services_history.py`, extend `test_dashboard_charts.py`, `tests/unit/test_dashboard_pages_trends.py`

**Interfaces:**
- Consumes: `service.get_rate_history_series(term_months, rate_category=None)`, `service.get_changes(days=…, term_months=…)`, `snapshot.term_market_from_rates` (Task 2).
- Produces:
  - `models.HistoryPoint(day: date, bank: str, rate_pct: float)`; `models.MarketPoint(day: date, median_pct: float, leader_pct: float, spread_bps: float, leaders: tuple[str,...])`; `models.TimelineEvent(detected_at: datetime, bank: str, change_bps: float, offset_days: float)`; `models.HistoryData(points: tuple[HistoryPoint,...], market: tuple[MarketPoint,...], timeline: tuple[TimelineEvent,...])`
  - `DashboardServices.history(term_months: int, banks: tuple[str,...], days: Optional[int]) -> Result[HistoryData]` (`days=None` = all)
  - `charts.history_lines(points) -> alt.Chart`; `charts.market_trend(market, series: Literal["median","leader","spread"]) -> alt.Chart`; `charts.repricing_timeline(events) -> alt.Chart`

- [ ] **Step 1: Failing service tests** — `tests/unit/test_dashboard_services_history.py`:

```python
from datetime import date

from test_dashboard_services_market import make_services


def test_history_series_and_market_stats_come_from_backend_math(market_factory):
    market = market_factory()
    data = make_services(market).history(12, ("ANZ", "BNZ"), days=None).value
    anz = [p for p in data.points if p.bank == "ANZ"]
    assert [(p.day, p.rate_pct) for p in anz][-2:] == [(date(2026, 10, 9), 4.89), (date(2026, 10, 10), 4.79)]
    assert {p.bank for p in data.points} == {"ANZ", "BNZ"}  # bank filter applies to lines only
    latest = data.market[-1]
    assert (latest.median_pct, latest.leader_pct, latest.spread_bps) == (4.85, 4.79, 10.0)
    assert latest.leaders == ("ANZ",)


def test_timeline_is_a_factual_sequence_with_offsets(market_factory):
    market = market_factory()
    events = make_services(market).history(12, ("ANZ",), days=30).value.timeline
    assert [(e.bank, e.change_bps, e.offset_days) for e in events] == [("ANZ", -10.0, 0.0)]


def test_history_not_blanked_by_term_deposit_withdrawal(market_factory, make_record):
    """Ported: a different product's withdrawal must not blank the mortgage history."""
    from balance_pricing_engine.pricing_intelligence.store import ObservationStore
    market = market_factory()
    store = ObservationStore(market.database)
    td = make_record(bank="ANZ", rate_pct=4.0, product_type="term_deposit",
                     rate_category="term_deposit", observed_at=market.now)
    store.record_observations([td], run_id="td-1", observed_at=market.now, product_type="term_deposit")
    store.record_observations([], run_id="td-2", observed_at=market.now, product_type="term_deposit")
    store.close()
    assert make_services(market).history(12, ("ANZ",), days=None).value.points


def test_history_with_no_data_in_range_is_empty(market_factory):
    market = market_factory()
    assert make_services(market).history(60, ("ANZ",), days=30).status == "EMPTY"
```

Run → FAIL. (The TD test mirrors the old `test_history_chart_is_not_blanked_by_a_different_products_withdrawal` — open that file in git history (`git show master:tests/unit/test_dashboard_market_intelligence.py`) and match its exact setup if mine does not reproduce the original scenario.)

- [ ] **Step 2: Implement `history`**

```python
    def history(
        self, term_months: int, banks: tuple[str, ...], days: Optional[int]
    ) -> Result[HistoryData]:
        def build(service: PricingIntelligenceService) -> Result[HistoryData]:
            now = self._clock()
            series = service.get_rate_history_series(term_months=term_months)
            if days is not None:
                cutoff = (now - timedelta(days=days)).date()
                series = [(d, rates) for d, rates in series if d >= cutoff]
            if not series:
                return Result.empty("No rate history for this term and range yet.")
            points = tuple(
                HistoryPoint(day, bank, rates[bank])
                for day, rates in series for bank in banks if bank in rates
            )
            market = []
            for day, rates in series:
                if not rates:
                    continue
                tm = term_market_from_rates(term_months, rates)
                market.append(MarketPoint(day, tm.median_rate, tm.best_rate, tm.spread_bps, tm.market_leaders))
            moves = [
                c for c in service.get_changes(
                    days=days, term_months=term_months, now=now, scope_to_market=True
                )
                if c.is_rate_move and not c.suspicious
            ]
            moves.sort(key=lambda c: c.detected_at)
            first = moves[0].detected_at if moves else None
            timeline = tuple(
                TimelineEvent(c.detected_at, c.bank, c.change_bps,
                              round((c.detected_at - first).total_seconds() / 86400, 1))
                for c in moves
            )
            return Result.ok(HistoryData(points, tuple(market), timeline))

        return self._guard(build)
```

Run → PASS.

- [ ] **Step 3: Charts** — append to `test_dashboard_charts.py`:

```python
def test_history_lines_is_one_chart_with_a_bank_colour_channel():
    from datetime import date
    from balance_pricing_engine.dashboard.charts import history_lines
    from balance_pricing_engine.dashboard.models import HistoryPoint
    spec = history_lines([HistoryPoint(date(2026, 10, 1), "ANZ", 4.9), HistoryPoint(date(2026, 10, 1), "BNZ", 4.85)]).to_dict()
    assert spec["encoding"]["color"]["field"] == "bank"
    assert len(next(iter(spec["datasets"].values()))) == 2
```

Implement in `charts.py`:

```python
def history_lines(points: Sequence[HistoryPoint]) -> alt.Chart:
    frame = pd.DataFrame([{"day": p.day, "bank": p.bank, "rate": p.rate_pct} for p in points])
    return (
        alt.Chart(frame)
        .mark_line(point=True, interpolate="step-after")
        .encode(
            x=alt.X("day:T", title=None),
            y=alt.Y("rate:Q", title="Advertised rate (%)", scale=alt.Scale(zero=False)),
            color=alt.Color("bank:N", legend=alt.Legend(title=None)),
            tooltip=["bank", "day:T", alt.Tooltip("rate:Q", format=".2f")],
        )
    )


def market_trend(market: Sequence[MarketPoint], series: str) -> alt.Chart:
    field, title = {
        "median": ("median_pct", "Market median (%)"),
        "leader": ("leader_pct", "Market leader (%)"),
        "spread": ("spread_bps", "Spread (bps)"),
    }[series]
    frame = pd.DataFrame(
        [{"day": m.day, "value": getattr(m, field)} for m in market]
    )
    return (
        alt.Chart(frame)
        .mark_line(point=True, interpolate="step-after")
        .encode(
            x=alt.X("day:T", title=None),
            y=alt.Y("value:Q", title=title, scale=alt.Scale(zero=series == "spread")),
        )
    )


def repricing_timeline(events: Sequence[TimelineEvent]) -> alt.Chart:
    frame = pd.DataFrame(
        [{"when": e.detected_at, "bank": e.bank, "bps": e.change_bps} for e in events]
    )
    return (
        alt.Chart(frame)
        .mark_circle(size=120)
        .encode(
            x=alt.X("when:T", title=None),
            y=alt.Y("bank:N", title=None),
            color=alt.Color("bps:Q", legend=alt.Legend(title="bps")),
            tooltip=["bank", "when:T", "bps"],
        )
    )
```

(`series` as `str` keeps py39 simple; import `MarketPoint`, `TimelineEvent`, `HistoryPoint`.)

- [ ] **Step 4: Page** — test `tests/unit/test_dashboard_pages_trends.py`:

```python
def test_trends_renders_one_chart_for_all_banks_and_a_timeline(market_factory):
    market = market_factory()
    app = run_page("trends", market.database, market.now)
    assert not app.exception
    # one history chart + one timeline chart; never one chart per bank
    assert len(app.get("arrow_vega_lite_chart")) == 2
    timeline = app.dataframe[0].value
    assert timeline.iloc[0]["Bank"] == "ANZ" and timeline.iloc[0]["Movement"] == "-10 bps"
```

Implement `pages/trends.py`: controls — `st.multiselect("Banks", selected_banks, default=all, key="trends_banks")`, term selectbox, range selectbox (`30 days`, `90 days`, `1 year`, `All` → days `30/90/365/None`), rate-type caption "Advertised specials". Body: `history_lines`; `st.radio("Market series", ["Median", "Leader", "Spread"], horizontal=True)` → `market_trend`; "Repricing timeline" subheader with `repricing_timeline` and a dataframe `Time, Bank, Movement, Days after first mover`. Caption: "Sequence of recorded rate moves; order does not imply causation."

Run all → PASS; lint/format/mypy; commit `feat: Historical Trends page`.

**Acceptance:** one multi-bank chart; median/leader/spread history equals backend `TermMarket` values; timeline is factual with no causal wording.

---

### Task 7: Pricing Scenarios page (and Scenario Lab)

**Files:**
- Modify: `pricing_intelligence/strategy.py` (add `apply_manual_competitor_rate_to_config`), `dashboard/models.py`, `dashboard/services.py` (`scenarios`), `dashboard/charts.py` (`objective_curve`), `tests/unit/test_pricing_intelligence_strategy.py` (append)
- Create: `dashboard/scenario_text.py`; replace stub `pages/scenarios.py`; finalise `pages/scenario_lab.py`
- Test: `tests/unit/test_dashboard_services_scenarios.py`, `tests/unit/test_dashboard_scenario_text.py`, `tests/unit/test_dashboard_pages_scenarios.py`, `tests/unit/test_dashboard_pages_scenario_lab.py`

**Interfaces:**
- Consumes: `service.get_market_snapshot`, `service.resolve_competitor_assumption`, `service.get_competitive_position`, `strategy.apply_market_strategy_to_config`, `pricing_scenario_trigger.run_triggered_scenarios(base_config, *, term_months, snapshot) -> ScenarioComparison`, `scenarios.runner.run_baseline_diagnostics(config)`, `EngineConfig`.
- Produces:
  - `strategy.apply_manual_competitor_rate_to_config(config, rate_fraction: float, set_reference_rate: bool = True)` — returns a **new** config with `market.competitor_loan_rate` set (and `home_loan_reference_rate` moved too when it is not `None` and `set_reference_rate`), validating finite/non-negative like `resolve_competitor_rate`.
  - `models.ScenarioRequest(term_months: int, strategy: str, manual_rate_pct: Optional[float], offset_bps: float, current_rate_pct: Optional[float])` (frozen; `strategy` is a `CompetitorRateStrategy` value string)
  - `models.ScenarioRowView(label, strategy, competitor_rate: float, our_rate: float, objective: float)` (fractions); `models.CurvePoint(rate: float, objective: float)`; `models.SelectedScenario(our_rate, objective, expected_loan_volume, funding_shortfall, competitor_rate, rationale: str)`; `models.PositionView(rank: int, bps_vs_best: float, bps_vs_median: float, market_best_pct: float, market_median_pct: float, bank_count: int)`; `models.ScenarioView(request, rows: tuple[ScenarioRowView,...], selected: SelectedScenario, curve: tuple[CurvePoint,...], position: Optional[PositionView], model_optimum_rate: Optional[float], excluded_banks: tuple[MarketExclusion,...])`
  - `DashboardServices.scenarios(request: ScenarioRequest) -> Result[ScenarioView]`; the engine config comes from `settings.engine_config_text`
  - `scenario_text.build_decision_text(view: ScenarioView) -> str`
  - `charts.objective_curve(curve, markers: Mapping[str, float]) -> alt.Chart`

- [ ] **Step 1: Backend manual-rate seam (RED→GREEN)** — append to `test_pricing_intelligence_strategy.py`:

```python
def test_apply_manual_competitor_rate_returns_a_new_config_and_moves_reference():
    from pathlib import Path
    import yaml
    from balance_pricing_engine.config import EngineConfig
    from balance_pricing_engine.pricing_intelligence.strategy import apply_manual_competitor_rate_to_config

    base = EngineConfig(**yaml.safe_load(Path("configs/base.yaml").read_text()))
    updated = apply_manual_competitor_rate_to_config(base, 0.0509)
    assert updated is not base and updated.market.competitor_loan_rate == 0.0509
    assert updated.market.home_loan_reference_rate == 0.0509
    assert base.market.competitor_loan_rate == 0.0499  # original untouched
    with pytest.raises(MarketStrategyError):
        apply_manual_competitor_rate_to_config(base, -0.01)
```

(import `pytest`/`MarketStrategyError` as that file already does.) Implement in `strategy.py` after `apply_market_strategy_to_config`:

```python
def apply_manual_competitor_rate_to_config(config, rate_fraction: float, set_reference_rate: bool = True):
    """Return ``config`` with an operator-entered competitor loan rate (decimal, 0.0509).

    The manual counterpart of ``apply_market_strategy_to_config``: that function must keep
    returning the *same* config object under ``MANUAL`` (existing scenarios depend on it),
    so an explicit operator override lives here instead.
    """
    if not math.isfinite(rate_fraction) or rate_fraction < 0:
        raise MarketStrategyError("manual_rate must be finite and non-negative")
    updates = {"competitor_loan_rate": rate_fraction}
    if set_reference_rate and config.market.home_loan_reference_rate is not None:
        updates["home_loan_reference_rate"] = rate_fraction
    return config.model_copy(update={"market": config.market.model_copy(update=updates)})
```

Run strategy + boundary tests → PASS.

- [ ] **Step 2: Failing service tests (pass-through + equals-backend)** — `tests/unit/test_dashboard_services_scenarios.py`:

```python
from unittest.mock import patch

import pytest

from balance_pricing_engine.dashboard.models import ScenarioRequest
from test_dashboard_services_market import make_services

FAST = {"max_boundary_expansions": 0}


@pytest.fixture
def fast_scenarios(monkeypatch):
    """Keep optimiser runs small: patch the grid expansion count, not the maths."""
    import balance_pricing_engine.pricing_scenario_trigger as trigger
    original = trigger.run_triggered_scenarios
    monkeypatch.setattr(
        trigger, "run_triggered_scenarios",
        lambda *a, **k: original(*a, **{**k, "max_boundary_expansions": 0}),
    )


def test_request_fields_reach_the_backend_unchanged(market_factory):
    market = market_factory()
    services = make_services(market)
    request = ScenarioRequest(12, "MARKET_LEADER", None, -5.0, 4.89)
    with patch("balance_pricing_engine.dashboard.services.run_triggered_scenarios") as trig, \
         patch("balance_pricing_engine.dashboard.services.run_baseline_diagnostics") as diag, \
         patch("balance_pricing_engine.dashboard.services.apply_market_strategy_to_config") as apply_:
        trig.side_effect = RuntimeError("stop")  # we only assert arguments
        services.scenarios(request)
    kwargs = trig.call_args.kwargs
    assert kwargs["term_months"] == 12
    assert kwargs["snapshot"].term(12).market_leaders == ("ANZ",)


def test_selected_scenario_uses_strategy_term_and_offset(market_factory):
    market = market_factory()
    services = make_services(market)
    request = ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None)
    with patch("balance_pricing_engine.dashboard.services.apply_market_strategy_to_config",
               wraps=__import__("balance_pricing_engine.pricing_intelligence.strategy", fromlist=["x"]).apply_market_strategy_to_config) as apply_, \
         patch("balance_pricing_engine.dashboard.services.run_triggered_scenarios",
               return_value=__import__("balance_pricing_engine.pricing_scenario_trigger", fromlist=["x"]).ScenarioComparison(12, ())):
        services.scenarios(request)
    call = apply_.call_args
    assert call.kwargs["strategy"] == "MARKET_MEDIAN" or str(call.kwargs["strategy"]) == "MARKET_MEDIAN"
    assert call.kwargs["term_months"] == 12 and call.kwargs["offset_bps"] == 0.0


def test_displayed_rows_equal_a_direct_backend_call(market_factory, fast_scenarios):
    from balance_pricing_engine.config import EngineConfig
    from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
    from balance_pricing_engine.pricing_intelligence.store import ObservationStore
    from balance_pricing_engine.pricing_scenario_trigger import run_triggered_scenarios
    import yaml

    market = market_factory()
    services = make_services(market)
    view = services.scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, 4.89)).value
    with PricingIntelligenceService(config=market.config, store=ObservationStore(market.database)) as svc:
        snapshot = svc.get_market_snapshot(now=market.now)
    base = EngineConfig(**yaml.safe_load(services.settings.engine_config_text))
    truth = run_triggered_scenarios(base, term_months=12, snapshot=snapshot, max_boundary_expansions=0)
    assert [(r.label, r.our_rate, r.competitor_rate, r.objective) for r in view.rows] == [
        (r.label, r.our_rate, r.competitor_rate, r.objective) for r in truth.rows
    ]
    assert view.model_optimum_rate == truth.model_optimum.our_rate
    assert view.position.rank == 4 and view.position.bps_vs_best == 10.0  # 4.89 vs leader 4.79


def test_manual_strategy_uses_the_entered_rate(market_factory, fast_scenarios):
    market = market_factory()
    view = make_services(market).scenarios(ScenarioRequest(12, "MANUAL", 5.09, 0.0, None)).value
    assert view.selected.competitor_rate == pytest.approx(0.0509)


def test_missing_manual_rate_is_an_operational_error(market_factory):
    result = make_services(market_factory()).scenarios(ScenarioRequest(12, "MANUAL", None, 0.0, None))
    assert result.status == "ERROR" and "manual" in result.message.lower()


def test_stale_banks_are_reported_as_excluded_from_the_benchmark(market_factory, fast_scenarios):
    market = market_factory(stale_banks=("Westpac",))
    view = make_services(market).scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None)).value
    assert [e.bank for e in view.excluded_banks] == ["Westpac"]
```

(The patch-heavy first two tests pin argument pass-through; the third pins value equality. If patching `services.run_triggered_scenarios` requires importing it at module level in `services.py` — it does, see Step 3.) Run → FAIL.

- [ ] **Step 3: Implement `scenarios`** — in `services.py` add imports `yaml`, `EngineConfig`, `run_triggered_scenarios`, `run_baseline_diagnostics`, `apply_market_strategy_to_config`, `apply_manual_competitor_rate_to_config`, `CompetitorRateStrategy`, `MarketStrategyError`, and:

```python
    def scenarios(self, request: ScenarioRequest) -> Result[ScenarioView]:
        def build(service: PricingIntelligenceService) -> Result[ScenarioView]:
            now = self._clock()
            try:
                base = EngineConfig(**yaml.safe_load(self.settings.engine_config_text))
            except Exception:  # noqa: BLE001
                logger.exception("engine config unreadable")
                return Result.error("The engine config could not be read; check PRICING_WATCH_CONFIG.")
            snapshot = service.get_market_snapshot(now=now)
            if request.term_months not in snapshot.term_months:
                return Result.empty(f"No comparable market data at {request.term_months} months.")
            strategy = CompetitorRateStrategy(request.strategy)
            try:
                comparison = run_triggered_scenarios(
                    base, term_months=request.term_months, snapshot=snapshot
                )
                if strategy is CompetitorRateStrategy.MANUAL:
                    if request.manual_rate_pct is None:
                        raise MarketStrategyError("MANUAL needs a manual competitor rate.")
                    selected_config = apply_manual_competitor_rate_to_config(
                        base, request.manual_rate_pct / 100
                    )
                else:
                    selected_config = apply_market_strategy_to_config(
                        base, strategy=strategy, snapshot=snapshot,
                        term_months=request.term_months, offset_bps=request.offset_bps,
                    )
                diagnostics = run_baseline_diagnostics(selected_config)
            except MarketStrategyError as exc:
                return Result.error(f"Scenario could not be built: {exc}")
            except NotImplementedError as exc:
                return Result.error(f"Scenario option error: {exc}")
            except Exception:  # noqa: BLE001
                logger.exception("scenario run failed")
                return Result.error("The pricing scenario failed to run; see the log.")
            best = diagnostics.best_result
            grid = diagnostics.grid_results
            curve = tuple(
                CurvePoint(float(rate), float(obj))
                for rate, obj in grid.groupby("loan_rate")["total_objective"].max().items()
            )
            position = None
            if request.current_rate_pct is not None:
                try:
                    pos = service.get_competitive_position(
                        request.current_rate_pct, term_months=request.term_months, now=now
                    )
                    position = PositionView(
                        pos.market_rank, pos.our_rate_minus_best_bps, pos.our_rate_minus_median_bps,
                        pos.market_best, pos.market_median, pos.bank_count,
                    )
                except ValueError:
                    position = None
            optimum = comparison.model_optimum
            return Result.ok(
                ScenarioView(
                    request=request,
                    rows=tuple(
                        ScenarioRowView(r.label, r.strategy, r.competitor_rate, r.our_rate, r.objective)
                        for r in comparison.rows
                    ),
                    selected=SelectedScenario(
                        our_rate=best.loan_rate, objective=best.total_objective,
                        expected_loan_volume=best.expected_loan_volume,
                        funding_shortfall=best.funding_shortfall,
                        competitor_rate=selected_config.market.competitor_loan_rate,
                        rationale=_rationale(strategy, request),
                    ),
                    curve=curve,
                    position=position,
                    model_optimum_rate=optimum.our_rate if optimum else None,
                    excluded_banks=tuple(service.get_market_exclusions(now=now)),
                )
            )

        return self._guard(build)
```

with `_rationale(strategy, request)` returning `f"{strategy.value} at {fmt_term} offset {offset:+.0f} bps"` (or "manual competitor rate X%"). (The curve reduction `groupby(...).max()` is a view of backend `grid_results`, not new economics — keep it that thin.) Run → PASS. Optimiser runs are slow (several seconds each); the full-economics tests use the `fast_scenarios` fixture; check total test time and keep it under ~60 s.

- [ ] **Step 4: Decision text** — `tests/unit/test_dashboard_scenario_text.py`:

```python
from balance_pricing_engine.dashboard.models import (
    PositionView, ScenarioRequest, ScenarioRowView, ScenarioView, SelectedScenario,
)
from balance_pricing_engine.dashboard.scenario_text import build_decision_text


def _view(position):
    rows = (
        ScenarioRowView("Match median", "MARKET_MEDIAN", 0.0485, 0.0484, 1_000_000.0),
        ScenarioRowView("Match leader", "MARKET_LEADER", 0.0479, 0.0478, 900_000.0),
        ScenarioRowView("Model optimum", "MODEL_OPTIMUM", 0.0485, 0.0484, 1_000_000.0),
    )
    return ScenarioView(
        ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, 4.89), rows,
        SelectedScenario(0.0484, 1_000_000.0, 1e8, 0.0, 0.0485, "x"), (), position, 0.0484, (),
    )


def test_text_states_gaps_optimum_and_the_disclaimer():
    text = build_decision_text(_view(PositionView(4, 10.0, 4.0, 4.79, 4.85, 5)))
    assert "10 bps above the market leader" in text and "4 bps above the market median" in text
    assert "model optimum is 4.84%" in text
    assert "Decision support only" in text


def test_text_without_a_current_rate_omits_position_claims():
    text = build_decision_text(_view(None))
    assert "above the market leader" not in text and "Decision support only" in text
```

Implement `scenario_text.py` (deterministic template; sentences only for available data):

```python
def build_decision_text(view: ScenarioView) -> str:
    lines = []
    p = view.position
    if p is not None:
        def gap(bps, ref):
            if bps == 0: return f"level with the {ref}"
            return f"{abs(bps):.0f} bps {'above' if bps > 0 else 'below'} the {ref}"
        lines.append(
            f"Current pricing is {gap(p.bps_vs_best, 'market leader')} and "
            f"{gap(p.bps_vs_median, 'market median')} (rank #{p.rank} of {p.bank_count})."
        )
    by_label = {r.label: r for r in view.rows}
    median, leader = by_label.get("Match median"), by_label.get("Match leader")
    if median and leader:
        delta = leader.objective - median.objective
        lines.append(
            f"Assuming competitors price at the market leader instead of the median changes the "
            f"modelled objective by {fmt_money(delta)} (optimal rate {fmt_rate_fraction(leader.our_rate)} "
            f"vs {fmt_rate_fraction(median.our_rate)})."
        )
    if view.model_optimum_rate is not None:
        lines.append(f"Under current assumptions, the model optimum is {fmt_rate_fraction(view.model_optimum_rate)}.")
    lines.append("Decision support only — this dashboard never changes a bank price.")
    return "\n\n".join(lines)
```

(Text deliberately avoids volume claims — the comparison rows carry no volume. Use `reporting.fmt_money`; check its signed-format output and adjust the expected substring if it renders differently — the tests above avoid asserting the money string.) Run → PASS.

- [ ] **Step 5: Objective curve chart + page** — chart test (append):

```python
def test_objective_curve_annotates_each_marker_once():
    from balance_pricing_engine.dashboard.charts import objective_curve
    from balance_pricing_engine.dashboard.models import CurvePoint
    spec = objective_curve(
        [CurvePoint(0.047, 1.0), CurvePoint(0.048, 2.0)],
        {"Current": 0.0489, "Market median": 0.0485, "Model optimum": 0.0484},
    ).to_dict()
    text = str(spec)
    for name in ("Current", "Market median", "Model optimum"):
        assert name in text
```

Implement `objective_curve`: a line of `(rate*100, objective)` plus `mark_rule` layers per marker with `text` labels; units: x "Our home-loan rate (%)", y "Modelled objective ($)". Page test — `test_dashboard_pages_scenarios.py` (stub the optimiser through the harness `setup` string so the page test is fast and deterministic):

```python
STUB = '''
from unittest.mock import patch
import pandas as pd
from balance_pricing_engine import pricing_scenario_trigger as trig
rows = (trig.ScenarioRow("No change","MANUAL",0.0499,0.0489,1e6),
        trig.ScenarioRow("Match median","MARKET_MEDIAN",0.0485,0.0484,1.1e6),
        trig.ScenarioRow("Model optimum","MODEL_OPTIMUM",0.0485,0.0484,1.1e6))
patch.object(trig, "run_triggered_scenarios", lambda *a, **k: trig.ScenarioComparison(12, rows)).start()
'''

def test_scenarios_page_shows_banner_rows_and_text(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now, setup=STUB)
    assert not app.exception
    assert any("Decision support only" in w.value for w in app.warning + app.info)
    app.number_input(key="scenario_current_rate").set_value(4.89)
    app.button(key="scenario_run").click().run()
    table = app.dataframe[0].value
    assert list(table["Scenario"]) == ["No-change optimum", "Match median", "Model optimum"]
    assert table.loc[1, "Our rate"] == "4.84%"
```

(Also stub `run_baseline_diagnostics` in the same `STUB` returning an object with `best_result`/`grid_results` — copy the `_fake_diagnostics` helper from the deleted `test_dashboard.py` via `git show master:tests/unit/test_dashboard.py` and extend `grid_results` with `loan_rate`/`total_objective` columns.) Implement `pages/scenarios.py`: persistent banner (`st.warning("Decision support only — this dashboard never changes a bank price.")`), inputs in a form: term select (from overview), strategy select (`MANUAL, MARKET_MEDIAN, MARKET_LEADER, MARKET_SECOND_LOWEST, MARKET_MEAN`), manual-rate `number_input` (percent; shown only for MANUAL), offset bps (hidden for MANUAL), `Current rate (%)` `number_input(key="scenario_current_rate")` with help "Your own advertised rate — used for market positioning only; the optimiser does not read it", and `st.button("Run scenario", key="scenario_run")`; the optimiser runs **only** on button click, result stored in `st.session_state["scenario_view"]` and re-rendered on reruns without recomputing. Display: excluded-bank warnings (`"{bank} excluded from the benchmark: latest validated observation is {age} h old (limit {threshold} h)."`), comparison table (`Scenario, Competitor, Our rate, Objective`, label `No change`→`No-change optimum`, rates via `fmt_rate_fraction`, objective via `fmt_money`), `objective_curve` with markers (`Current` only if provided; `Market median`/`Market leader` from `position`; `Model optimum`), `build_decision_text`, selected-scenario detail metrics (our rate, objective, expected loan volume, funding shortfall). A `st.tabs(["Scenarios", "Scenario Lab"])` wrapper calls `scenario_lab.render(services)` in the second tab.

- [ ] **Step 6: Scenario Lab tests (ported)** — `test_dashboard_pages_scenario_lab.py`: port the four old tests from `git show master:tests/unit/test_dashboard.py` (stops until Run clicked → `info`; config error → `error`; renders 6 metrics + 2 pyplot + warning + dataframe with monkeypatched diagnostics; unsupported sensitivity → `error`) onto `run_page("scenario_lab", …)` using the same monkeypatching style via the harness `setup` string. Run: `python3 -m pytest tests/unit/test_dashboard_services_scenarios.py tests/unit/test_dashboard_scenario_text.py tests/unit/test_dashboard_pages_scenarios.py tests/unit/test_dashboard_pages_scenario_lab.py tests/unit/test_dashboard_charts.py tests/unit/test_pricing_intelligence_strategy.py tests/unit/test_pricing_intelligence_boundaries.py -q` → PASS. Lint/format/mypy; commit `feat: Pricing Scenarios page and Scenario Lab on existing optimiser seams`.

**Acceptance:** term/strategy/manual rate/offset reach the backend unchanged; displayed rows equal a direct `run_triggered_scenarios` call; MANUAL/MARKET_MEDIAN/MARKET_LEADER work; optimiser never runs on rerender; labelled decision-support only.

---

### Task 8: Operations (actions, worker, JobRunner, page) and safety tests

**Files:**
- Create: `dashboard/actions.py`, `dashboard/run_worker.py`; replace stub `pages/operations.py`
- Test: `tests/unit/test_dashboard_actions.py`, `tests/unit/test_dashboard_pages_operations.py`

**Interfaces:**
- Consumes: `DashboardSettings`; `backup.backup_database`; `alerts.build_telegram_sink_from_env`, `plain_alert`; `orchestration.run_operational_cycle`, `build_summary`, `EXIT_*`; `ObservationStore(read_only=True)`; `scripts/collect_market_rates_workflow.py` CLI (`--config --database --render-javascript --summary-file --quiet`; exit 0/1/2/3).
- Produces (`actions.py`, no Streamlit):
  - `@dataclass(frozen=True) BankOutcome(bank: str, ok: bool, detail: str)`
  - `@dataclass(frozen=True) ActionResult(action: str, status: str, message: str, completed_at: datetime, elapsed_seconds: float = 0.0, banks: tuple[BankOutcome,...] = (), summary: Mapping[str, object] = {})` with `status ∈ {"SUCCESS","PARTIAL_SUCCESS","FAILED","ALREADY_RUNNING","NOT_CONFIGURED","TIMED_OUT"}`
  - `run_live(settings, *, runner=subprocess.run, clock=_utcnow, timeout_seconds=1800) -> ActionResult`
  - `run_dry(settings, *, runner=subprocess.run, clock=_utcnow, timeout_seconds=1800) -> ActionResult`
  - `create_backup(settings, *, clock=_utcnow) -> ActionResult`
  - `send_test_notification(*, sink_factory=build_telegram_sink_from_env, clock=_utcnow) -> ActionResult`
  - `class JobRunner`: `submit(name: str, fn: Callable[[], ActionResult]) -> bool` (False if one is running), `state() -> JobState` (`JobState(name: Optional[str], running: bool, result: Optional[ActionResult])`), `wait(timeout: float = 5.0) -> None` (tests), `clear_result() -> None`; module singleton `default_runner() -> JobRunner`
  - `run_worker.main(argv) -> int` — `python -m balance_pricing_engine.dashboard.run_worker --config PATH --database PATH --result-file PATH [--render-javascript]`

- [ ] **Step 1: Failing action tests** — `tests/unit/test_dashboard_actions.py`:

```python
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from balance_pricing_engine.dashboard import actions
from balance_pricing_engine.dashboard.settings import load_settings

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
clock = lambda: NOW  # noqa: E731


def fake_runner(exit_code, summary=None, calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        if summary is not None:
            Path(cmd[cmd.index("--summary-file") + 1]).write_text(json.dumps(summary))
        return subprocess.CompletedProcess(cmd, exit_code, stdout="", stderr="")
    return run


@pytest.fixture
def settings(tmp_path):
    return load_settings({}, database_path=str(tmp_path / "m.db"))


SUMMARY = {
    "banks_succeeded": ["ASB", "ANZ"], "banks_failed": ["Westpac"], "rates_accepted": 14,
    "changes_detected": 2, "suspicious_changes": 0,
    "failures": [{"bank": "Westpac", "error_type": "ConnectionError", "error": "boom"}],
}


def test_live_run_launches_the_scheduled_workflow_once_with_render_javascript(settings):
    calls = []
    result = actions.run_live(settings, runner=fake_runner(2, SUMMARY, calls), clock=clock)
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[1].endswith("scripts/collect_market_rates_workflow.py")
    assert "--render-javascript" in cmd and "--summary-file" in cmd
    assert cmd[cmd.index("--database") + 1] == settings.intelligence.database_path
    assert result.status == "PARTIAL_SUCCESS"
    assert {b.bank: b.ok for b in result.banks} == {"ASB": True, "ANZ": True, "Westpac": False}
    assert result.summary["changes_detected"] == 2


@pytest.mark.parametrize(
    "code,status", [(0, "SUCCESS"), (1, "FAILED"), (2, "PARTIAL_SUCCESS"), (3, "ALREADY_RUNNING"), (9, "FAILED")]
)
def test_exit_codes_map_to_statuses(settings, code, status):
    assert actions.run_live(settings, runner=fake_runner(code, SUMMARY), clock=clock).status == status


def test_unreadable_summary_still_reports_the_exit_status(settings):
    result = actions.run_live(settings, runner=fake_runner(0, None), clock=clock)
    assert result.status == "SUCCESS" and "summary" in result.message.lower()


def test_timeout_is_reported_not_raised(settings):
    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    assert actions.run_live(settings, runner=hang, clock=clock).status == "TIMED_OUT"


def test_dry_run_uses_the_worker_and_never_the_workflow_script(settings):
    calls = []
    summary = {"status": "SUCCESS", "summary": SUMMARY}
    def runner(cmd, **kw):
        calls.append(cmd)
        Path(cmd[cmd.index("--result-file") + 1]).write_text(json.dumps(summary))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    result = actions.run_dry(settings, runner=runner, clock=clock)
    assert "balance_pricing_engine.dashboard.run_worker" in calls[0]
    assert not any("collect_market_rates_workflow" in part for part in calls[0])
    assert result.status == "SUCCESS" and "nothing was persisted" in result.message.lower()


def test_backup_reports_a_safe_relative_location(settings):
    from balance_pricing_engine.pricing_intelligence.store import ObservationStore
    ObservationStore(settings.intelligence.database_path).close()
    result = actions.create_backup(settings, clock=clock)
    assert result.status == "SUCCESS" and Path(result.summary["path"]).exists()
    assert str(settings.backup_dir) in result.summary["path"]


def test_backup_without_a_database_fails_gracefully(tmp_path):
    result = actions.create_backup(load_settings({}, database_path=str(tmp_path / "x.db")), clock=clock)
    assert result.status == "FAILED" and "no database" in result.message.lower()


def test_telegram_not_configured_is_explained_without_credentials():
    result = actions.send_test_notification(sink_factory=lambda: None, clock=clock)
    assert result.status == "NOT_CONFIGURED"
    assert "TELEGRAM" not in result.message.upper().replace("TELEGRAM IS NOT CONFIGURED", "")


def test_telegram_failure_never_leaks_the_bot_token(caplog):
    class Leaky:
        def send(self, alert):
            raise RuntimeError("POST https://api.telegram.org/bot123456:SECRET/sendMessage failed")
    result = actions.send_test_notification(sink_factory=Leaky, clock=clock)
    assert result.status == "FAILED"
    assert "SECRET" not in result.message
    assert all("SECRET" not in r.getMessage() for r in caplog.records)
    assert all("SECRET" not in str(r.exc_info) for r in caplog.records if r.exc_info)


def test_telegram_success():
    sent = []
    class Ok:
        def send(self, alert):
            sent.append(alert)
    assert actions.send_test_notification(sink_factory=Ok, clock=clock).status == "SUCCESS"
    assert len(sent) == 1


def test_job_runner_is_single_flight():
    import threading
    gate, runs = threading.Event(), []
    def slow():
        runs.append(1)
        gate.wait(2)
        return actions.ActionResult("live", "SUCCESS", "ok", NOW)
    runner = actions.JobRunner()
    assert runner.submit("live", slow) is True
    assert runner.submit("live", slow) is False  # double-click / second tab
    assert runner.state().running
    gate.set()
    runner.wait()
    assert runs == [1] and runner.state().result.status == "SUCCESS"
    assert runner.submit("live", slow) is True  # free again once finished
```

(Note the Leaky test: `logger.exception` would attach a traceback containing the secret. The implementation must log only `type(exc).__name__` via `logger.error("telegram test failed: %s", type(exc).__name__)` — no `exc_info`.) Run → FAIL.

- [ ] **Step 2: Implement `actions.py`**

```python
"""Operational actions. No Streamlit imports; nothing here is reachable from a read path.

Live collection runs as a subprocess of the *scheduled* workflow script so it shares its
single-instance lock, exit codes, scenario trigger and JSON summary, and so Playwright never
runs inside Streamlit's script thread.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from balance_pricing_engine.dashboard.settings import DashboardSettings
from balance_pricing_engine.pricing_intelligence.alerts import build_telegram_sink_from_env, plain_alert
from balance_pricing_engine.pricing_intelligence.backup import backup_database

logger = logging.getLogger(__name__)

WORKFLOW_SCRIPT = Path("scripts") / "collect_market_rates_workflow.py"
_STATUS_BY_EXIT = {0: "SUCCESS", 1: "FAILED", 2: "PARTIAL_SUCCESS", 3: "ALREADY_RUNNING"}
_MESSAGES = {
    "SUCCESS": "Pricing Watch run completed.",
    "PARTIAL_SUCCESS": "Run finished but some banks failed; their last rates are marked stale. See System Health.",
    "FAILED": "Run failed; stored data is unchanged. See System Health and the application log.",
    "ALREADY_RUNNING": "Another Pricing Watch run is already in progress; nothing was started.",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BankOutcome:
    bank: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class ActionResult:
    action: str
    status: str
    message: str
    completed_at: datetime
    elapsed_seconds: float = 0.0
    banks: tuple[BankOutcome, ...] = ()
    summary: Mapping[str, object] = field(default_factory=dict)


def _env(settings: DashboardSettings) -> dict:
    src = str(settings.repo_root / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _banks_from_summary(summary: Mapping[str, object]) -> tuple[BankOutcome, ...]:
    failures = {f.get("bank"): f.get("error_type", "") for f in summary.get("failures", []) or []}
    ok = [BankOutcome(b, True) for b in summary.get("banks_succeeded", []) or []]
    bad = [BankOutcome(b, False, str(failures.get(b, ""))) for b in summary.get("banks_failed", []) or []]
    return tuple(ok + bad)


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _run_process(
    action: str, command: list[str], result_path: Path, settings: DashboardSettings,
    runner: Callable, clock: Callable[[], datetime], timeout_seconds: float, *, summary_key: Optional[str],
    dry: bool,
) -> ActionResult:
    started = time.monotonic()
    try:
        completed = runner(
            command, capture_output=True, text=True, timeout=timeout_seconds,
            cwd=str(settings.repo_root), env=_env(settings), check=False,
        )
    except subprocess.TimeoutExpired:
        return ActionResult(action, "TIMED_OUT", "The run exceeded its time limit and was stopped.", clock(),
                            time.monotonic() - started)
    except OSError:
        logger.exception("could not launch %s", action)
        return ActionResult(action, "FAILED", "The run could not be started; see the log.", clock(), 0.0)
    elapsed = time.monotonic() - started
    status = _STATUS_BY_EXIT.get(completed.returncode, "FAILED")
    payload = _read_json(result_path)
    summary = (payload.get(summary_key) if payload and summary_key else payload) or {}
    if dry and payload and payload.get("status"):
        status = payload["status"]
    message = _MESSAGES.get(status, _MESSAGES["FAILED"])
    if not summary:
        message += " (Run summary could not be read.)"
    if dry:
        message += " Dry run: nothing was persisted, no alerts were sent, collector health was not updated."
    return ActionResult(action, status, message, clock(), elapsed, _banks_from_summary(summary), summary)
```

Continue the module:

```python
def run_live(settings, *, runner=subprocess.run, clock=_utcnow, timeout_seconds=1800) -> ActionResult:
    with tempfile.TemporaryDirectory() as tmp:
        result_path = Path(tmp) / "summary.json"
        command = [
            sys.executable, str(settings.repo_root / WORKFLOW_SCRIPT),
            "--config", str(settings.engine_config_path),
            "--database", settings.intelligence.database_path,
            "--render-javascript", "--quiet", "--summary-file", str(result_path),
        ]
        return _run_process("live", command, result_path, settings, runner, clock, timeout_seconds,
                            summary_key=None, dry=False)


def run_dry(settings, *, runner=subprocess.run, clock=_utcnow, timeout_seconds=1800) -> ActionResult:
    with tempfile.TemporaryDirectory() as tmp:
        result_path = Path(tmp) / "result.json"
        command = [
            sys.executable, "-m", "balance_pricing_engine.dashboard.run_worker",
            "--config", str(settings.engine_config_path),
            "--database", settings.intelligence.database_path,
            "--render-javascript", "--result-file", str(result_path),
        ]
        return _run_process("dry_run", command, result_path, settings, runner, clock, timeout_seconds,
                            summary_key="summary", dry=True)


def create_backup(settings, *, clock=_utcnow) -> ActionResult:
    database = Path(settings.intelligence.database_path)
    if not database.exists():
        return ActionResult("backup", "FAILED", "No database to back up yet.", clock())
    try:
        path = backup_database(database, settings.backup_dir, now=clock())
    except Exception:  # noqa: BLE001
        logger.exception("backup failed")
        return ActionResult("backup", "FAILED", "Backup failed; see the log.", clock())
    return ActionResult("backup", "SUCCESS", "Backup created.", clock(), summary={"path": str(path)})


def send_test_notification(*, sink_factory=build_telegram_sink_from_env, clock=_utcnow) -> ActionResult:
    sink = sink_factory()
    if sink is None:
        return ActionResult("telegram_test", "NOT_CONFIGURED",
                            "Telegram is not configured for this environment.", clock())
    try:
        sink.send(plain_alert("NZ Pricing Intelligence Telegram test successful.", ""))
    except Exception as exc:  # noqa: BLE001 - class name only: messages can embed the bot-token URL
        logger.error("telegram test failed: %s", type(exc).__name__)
        return ActionResult("telegram_test", "FAILED", "Telegram test failed; check the bot and chat settings.", clock())
    return ActionResult("telegram_test", "SUCCESS", "Test notification sent.", clock())


@dataclass(frozen=True)
class JobState:
    name: Optional[str]
    running: bool
    result: Optional[ActionResult]


class JobRunner:
    """Single-flight background runner: at most one long job per server process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._name: Optional[str] = None
        self._result: Optional[ActionResult] = None

    def submit(self, name: str, fn: Callable[[], ActionResult]) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._name, self._result = name, None
            self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
            self._thread.start()
            return True

    def _run(self, fn: Callable[[], ActionResult]) -> None:
        try:
            result = fn()
        except Exception:  # noqa: BLE001
            logger.exception("background job crashed")
            result = ActionResult(self._name or "job", "FAILED", "The job crashed; see the log.", _utcnow())
        with self._lock:
            self._result = result

    def state(self) -> JobState:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return JobState(self._name, running, self._result)

    def wait(self, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def clear_result(self) -> None:
        with self._lock:
            self._result = None


_DEFAULT = JobRunner()


def default_runner() -> JobRunner:
    return _DEFAULT
```

Implement `run_worker.py`:

```python
"""Dry-run worker: one non-persisting operational cycle, result as JSON.

Run as a subprocess by ``actions.run_dry`` so live page rendering (Playwright) stays out of
Streamlit's process. Persists nothing: the store is read-only and alerts go to the console.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard.settings import resolve_intelligence_config
from balance_pricing_engine.pricing_intelligence.orchestration import build_summary, run_operational_cycle
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--render-javascript", action="store_true")
    args = parser.parse_args(argv)

    text = Path(args.config).read_text(encoding="utf-8") if Path(args.config).exists() else ""
    config = resolve_intelligence_config(text).model_copy(update={"database_path": args.database})
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path, read_only=True),
        render_javascript=args.render_javascript,
    ) as service:
        result = run_operational_cycle(service, product_type=ProductType.HOME_LOAN, dry_run=True)
    Path(args.result_file).write_text(
        json.dumps({"status": result.status, "summary": build_summary(result.outcome, ProductType.HOME_LOAN)}),
        encoding="utf-8",
    )
    return result.code


if __name__ == "__main__":
    raise SystemExit(main())
```

Run `python3 -m pytest tests/unit/test_dashboard_actions.py -v` → PASS. Add a worker test: run `run_worker.main([...])` in-process with a patched `PricingIntelligenceService.collect`? Simplest: `monkeypatch.setattr(run_worker, "run_operational_cycle", lambda *a, **k: SimpleNamespace(status="SUCCESS", code=0, outcome=<outcome from fixture_fetcher collect>))` is brittle — instead build a real outcome with `fixture_fetcher`: construct service with `fetcher=fixture_fetcher()` is not injectable via `main`. Therefore test the worker's contract with `monkeypatch.setattr(run_worker, "PricingIntelligenceService", FakeService)` where `FakeService.collect` returns a real `CollectionOutcome` made by a real service over `fixture_fetcher` with `persist=False`; assert the result file has `status`/`summary` keys and **the database file was not created/modified** (`assert not db.exists()`).

- [ ] **Step 3: Failing safety tests for the page (AppTest)** — `tests/unit/test_dashboard_pages_operations.py`:

```python
from unittest.mock import patch

from balance_pricing_engine.dashboard import actions
from dashboard_harness import run_page

OK = lambda name: actions.ActionResult(name, "SUCCESS", "done", __import__("datetime").datetime(2026, 10, 11, tzinfo=__import__("datetime").timezone.utc))


def test_rendering_and_refresh_never_collect(market_factory):
    market = market_factory()
    with patch.object(actions, "run_live") as live, patch.object(actions, "run_dry") as dry:
        app = run_page("operations", market.database, market.now)
        for _ in range(3):
            app.run()
        app.button(key="refresh_dashboard").click().run()
        assert not app.exception
    assert live.call_count == 0 and dry.call_count == 0


def test_run_requires_confirmation_then_launches_exactly_once(market_factory):
    market = market_factory()
    with patch.object(actions, "run_live", side_effect=lambda *a, **k: OK("live")) as live:
        app = run_page("operations", market.database, market.now)
        assert app.button(key="run_live").disabled  # not confirmed yet
        app.checkbox(key="confirm_live").check().run()
        app.button(key="run_live").click().run()
        actions.default_runner().wait()
        app.run()
        app.run()  # extra reruns must not relaunch
    assert live.call_count == 1
    assert any("done" in s.value or "SUCCESS" in s.value for s in app.success + app.markdown)


def test_dry_run_button_calls_dry_run_only(market_factory):
    market = market_factory()
    with patch.object(actions, "run_dry", side_effect=lambda *a, **k: OK("dry_run")) as dry, \
         patch.object(actions, "run_live") as live:
        app = run_page("operations", market.database, market.now)
        app.button(key="run_dry").click().run()
        actions.default_runner().wait()
    assert dry.call_count == 1 and live.call_count == 0


def test_telegram_not_configured_message(market_factory, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    market = market_factory()
    app = run_page("operations", market.database, market.now)
    app.button(key="telegram_test").click().run()
    assert any("not configured" in w.value.lower() for w in app.warning)


def test_already_running_is_shown_not_launched_twice(market_factory):
    import threading
    gate = threading.Event()
    market = market_factory()
    def slow(*a, **k):
        gate.wait(2)
        return OK("live")
    with patch.object(actions, "run_live", side_effect=slow) as live:
        app = run_page("operations", market.database, market.now)
        app.checkbox(key="confirm_live").check().run()
        app.button(key="run_live").click().run()
        app.button(key="run_live").click().run()  # second click while running
        gate.set()
        actions.default_runner().wait()
    assert live.call_count == 1
```

(Each test must reset the singleton: add an autouse fixture in the file that does `actions.default_runner().wait(); actions.default_runner().clear_result()`.) Run → FAIL.

- [ ] **Step 4: Implement `pages/operations.py`**

```python
"""Operations: the only page that can start a collection."""

from __future__ import annotations

import streamlit as st

from balance_pricing_engine.dashboard import actions
from balance_pricing_engine.dashboard.formatting import fmt_nz_datetime
from balance_pricing_engine.dashboard.services import DashboardServices


def _show(result: actions.ActionResult) -> None:
    kind = {"SUCCESS": st.success, "PARTIAL_SUCCESS": st.warning}.get(result.status, st.error)
    if result.status in {"ALREADY_RUNNING", "NOT_CONFIGURED"}:
        kind = st.warning
    kind(f"{result.status}: {result.message}")
    st.caption(f"Completed {fmt_nz_datetime(result.completed_at)} · {result.elapsed_seconds:.0f} s")
    if result.banks:
        st.dataframe(
            [{"Bank": b.bank, "Result": "SUCCESS" if b.ok else "FAILED", "Detail": b.detail} for b in result.banks],
            hide_index=True, width="stretch",
        )
    summary = result.summary
    if "changes_detected" in summary:
        st.write(
            f"Rates accepted: {summary.get('rates_accepted', '—')} · "
            f"Changes detected: {summary.get('changes_detected', '—')} · "
            f"Suspicious: {summary.get('suspicious_changes', '—')}"
        )
    if "path" in summary:
        st.write(f"Backup written to `{summary['path']}`")


def render(services: DashboardServices) -> None:
    settings = services.settings
    runner = actions.default_runner()

    st.subheader("Refresh Dashboard")
    st.caption("Reads the latest **stored** data only. Never contacts a bank.")
    if st.button("Refresh Dashboard", key="refresh_dashboard"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("Run Pricing Watch")
    st.caption(
        "Collects live rates from every tracked bank website, validates, stores, detects changes "
        "and updates the market snapshot. Takes several minutes and sends alerts if configured."
    )
    confirmed = st.checkbox("I understand this contacts live bank websites", key="confirm_live")
    state = runner.state()
    if st.button("Run Pricing Watch", key="run_live", type="primary",
                 disabled=not confirmed or state.running):
        if not runner.submit("live", lambda: actions.run_live(settings)):
            st.warning("A run is already in progress; nothing was started.")
        state = runner.state()
    if st.button("Dry Run", key="run_dry", disabled=state.running):
        if not runner.submit("dry_run", lambda: actions.run_dry(settings)):
            st.warning("A run is already in progress; nothing was started.")
        state = runner.state()
    st.caption(
        "Dry Run collects live pages and classifies them as normal, but persists nothing, "
        "sends no alerts and leaves collector health untouched."
    )

    _job_status(runner)

    st.divider()
    st.subheader("Maintenance")
    left, right = st.columns(2)
    if left.button("Create Backup", key="create_backup"):
        _show(actions.create_backup(settings))
    if right.button("Send Test Notification", key="telegram_test"):
        _show(actions.send_test_notification())


@st.fragment(run_every="3s")
def _job_status(runner: actions.JobRunner) -> None:
    state = runner.state()
    if state.running:
        st.info(f"Running: {state.name}… this page updates automatically.")
    elif state.result is not None:
        _show(state.result)
        if state.name == "live" and state.result.status in {"SUCCESS", "PARTIAL_SUCCESS"}:
            st.cache_data.clear()  # new data is stored; next read must not serve the old cache
```

(`st.button(...)` inside `if` for ops runs on the click's rerun only — a plain rerender does not re-enter that branch. The fragment's `run_every` keeps polling only while the page is open; if AppTest rejects `run_every`, call `_job_status` as a plain function and add `st.autorefresh`-free manual "Refresh status" button — record the choice.) Run → PASS. Add the post-run per-bank record-count enrichment later only if time allows (spec says per-bank counts; the summary has totals — **state this limitation in docs, not UI invention**).

- [ ] **Step 5: `.gitignore` check and commit**

Run: `git check-ignore -v data/processed/pricing_intelligence/backups/x.sqlite3` → Expected: ignored (covered by `data/processed/pricing_intelligence/`). Lint/format/mypy new files. Commit:

```bash
git add src tests && git commit -m "feat: Operations page with single-flight live/dry runs, backup and Telegram test" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

**Acceptance:** Refresh/rerender/navigation never launch; confirm gate; exactly-once launch incl. double click; exit-code mapping incl. already-running; Telegram token never surfaces; dry run never touches the DB file.

---

### Task 9: System Health page

**Files:**
- Modify: `dashboard/models.py` (`SystemHealthView`, `RunRow`, `RunDetail`, `SuspiciousRow`), `dashboard/services.py` (`health`, `runs`, `run_detail`, `suspicious`)
- Replace: `pages/system_health.py`
- Test: `tests/unit/test_dashboard_services_health.py`, `tests/unit/test_dashboard_pages_system_health.py`

**Interfaces:**
- Consumes: `assess_system_health(service, product_type=…, now=…)`, `store.get_recent_run_records`, `store.get_run_record`, `service.get_changes(...)` (suspicious flag), `store.get_current_observations` (warning-status rows), `store.get_recent_scenario_runs`.
- Produces: `DashboardServices.health(product_type=ProductType.HOME_LOAN) -> Result[SystemHealth]` (returns the backend `SystemHealth` unchanged — **never cached**), `.runs(product_type, limit=20) -> Result[tuple[RunRow,...]]`, `.run_detail(run_id) -> Result[RunDetail]`, `.suspicious() -> Result[tuple[SuspiciousRow,...]]`; `RunRow(run_id, started_at, status, banks_ok: int, banks_failed: int, observations: int, changes: int, duration_seconds: float)`; `RunDetail(row: RunRow, banks_successful: tuple[str,...], banks_failed: tuple[str,...], validation_failures: int, suspicious: int, market_state: Optional[str], error_summary: Optional[str])`; `SuspiciousRow(bank, term_months: Optional[int], previous_pct: Optional[float], observed_pct: Optional[float], change_bps: Optional[float], detected_at)`.

- [ ] **Step 1: Failing tests** — services:

```python
def test_health_reports_backend_freshness_not_a_recomputation(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    health = make_services(market).health().value
    westpac = next(b for b in health.banks if b.bank == "Westpac")
    assert westpac.freshness.state.value == "STALE" and westpac.consecutive_failures == 2
    assert health.max_observation_age_hours == 168.0


def test_runs_and_detail(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    services = make_services(market)
    rows = services.runs().value
    assert [r.status for r in rows][0] == "PARTIAL_SUCCESS"  # newest first
    assert rows[0].banks_failed == 1 and rows[0].duration_seconds == 120.0
    detail = services.run_detail(rows[0].run_id).value
    assert detail.banks_failed == ("Westpac",)


def test_suspicious_observations_are_separated(market_factory, make_record):
    from balance_pricing_engine.pricing_intelligence.store import ObservationStore
    market = market_factory(anz_new_rate=8.49)  # +360 bps: validation flags it suspicious
    rows = make_services(market).suspicious().value
    assert rows and rows[0].bank == "ANZ" and rows[0].change_bps == 360.0
    overview = make_services(market).overview().value
    assert overview.matrix.cells[("ANZ", 12)].rate_pct == 4.89  # trusted rate untouched
```

(If the last assertion shows the suspicious value leaking into the trusted matrix, that is a **real defect** — debug with `systematic-debugging` against `get_market_snapshot`'s documented fallback-to-last-confirmed behaviour before changing anything.) Page tests (`test_dashboard_pages_system_health.py`): component table shows Database `HEALTHY`/OK, "Last run" with NZ time; collector table has columns `Bank, Last success, Age, Failures, Freshness, Health, Current records`; stale bank row reads `⚠ STALE`/`⚠ DEGRADED` with the line "Westpac excluded from market statistics: …"; run history dataframe and a `selectbox(key="run_detail")` drill-down; suspicious section header `Excluded from trusted benchmark` rendered only when rows exist and separate from collector table; term-deposit sections (`Collector health (Term deposit)`, `Recent runs (Term deposit)`) still render — port the old `test_term_deposit_collector_activity_appears_in_its_own_operational_section` setup from `git show master:tests/unit/test_dashboard_market_intelligence.py`; empty DB shows guidance without exceptions; corrupt DB shows `st.error`.

- [ ] **Step 2: Implement** the four service methods (each `_guard`ed; `health` builds `assess_system_health(service, product_type=…, now=self._clock())`; `runs` maps `RunRecord` → `RunRow` with `duration_seconds = (completed_at - started_at).total_seconds()`; `suspicious` = `service.get_changes(days=None, now=now, scope_to_market=True)` filtered `c.suspicious and c.is_rate_move`) and the page, which calls `services.health()` **directly (not `cached`)**. Scheduler row: `Last run: {fmt_nz_datetime(latest.started_at)} ({status}) — {age} h ago`; add the caption "Pricing Watch does not observe the external scheduler; last-run age is the only signal." Operational failure message when any bank is not HEALTHY: `"{bank} collection failed. Its last validated observation remains available but is marked STALE. See below."` — only when `collector is FAILING` and freshness STALE; otherwise a neutral "needs review" line.

Run services + page tests → PASS. Lint/format/mypy; commit `feat: System Health page`.

**Acceptance:** freshness/health on the page are the backend's values; failed bank is DEGRADED/STALE with explanation; suspicious rows never share a table with trusted rates; deposit tables retained.

---

### Task 10: Boundaries, end-to-end flows, docs, review and verification

**Files:**
- Create: `tests/unit/test_dashboard_boundaries.py`, `tests/unit/test_dashboard_e2e_flows.py`
- Modify: `README.md`, `docs/pricing_intelligence.md` (Dashboard section ~line 410), `docs/architecture.md` (module map + one paragraph), `AGENTS.md`, `CHANGELOG.md`

- [ ] **Step 1: Architecture tests** — `tests/unit/test_dashboard_boundaries.py`:

```python
from pathlib import Path

import pytest

DASH = Path("src/balance_pricing_engine/dashboard")
NO_STREAMLIT = ["services", "actions", "formatting", "charts", "models", "results", "settings",
                "scenario_text", "run_worker"]


@pytest.mark.parametrize("module", NO_STREAMLIT)
def test_ui_free_modules_do_not_import_streamlit(module):
    assert "import streamlit" not in (DASH / f"{module}.py").read_text()


def test_pages_do_not_recompute_pricing_intelligence():
    forbidden = ("statistics", "median(", "sorted(rates", ".rank_for(", "rate_change_bps",
                 "max_observation_age_hours", "timedelta(hours")
    offenders = [
        (p.name, token)
        for p in (DASH / "pages").glob("*.py") if p.name != "scenario_lab.py"
        for token in forbidden if token in p.read_text()
    ]
    assert offenders == []


def test_only_operations_reaches_actions():
    users = [p.name for p in (DASH / "pages").glob("*.py") if "dashboard import actions" in p.read_text()
             or "dashboard.actions" in p.read_text()]
    assert users == ["operations.py"]
    assert "actions" not in (DASH / "services.py").read_text().replace("# ", "")
```

(The page `operations.py` is allowed to use `max_observation_age_hours`? It does not. If the token list flags a legitimate use, narrow the token — do not weaken the intent.) Run → PASS.

- [ ] **Step 2: End-to-end flows** — `tests/unit/test_dashboard_e2e_flows.py`, one test per brief flow, using `market_factory` + the harness:
  - **A** Overview → matrix → term `1Y` → leader/median/spread/ranking equal `PricingIntelligenceService` values read directly.
  - **B** `market_factory(anz_new_rate=4.79)`: `changes()` shows `-10 bps`; ranking shows ANZ rank 1; `history()` last point 4.79 with the prior 4.89; matches spec example.
  - **C** scenario view rows equal a direct `run_triggered_scenarios` (reuse Task 7's helper) for Current/Market Median/Market Leader/Model optimum.
  - **D** with `actions.run_live` patched: Operations click launches once; afterwards Refresh Dashboard click → `run_live.call_count` still 1, and a spy on `ObservationStore.record_observations` never fires.
  - **E** `market_factory(stale_banks=("Westpac",))`: other four banks present in the matrix; Westpac cell stale-marked; `health()` DEGRADED; System Health page text explains; every page renders `not app.exception`.
Run → PASS.

- [ ] **Step 3: Docs** — `docs/pricing_intelligence.md` Dashboard section: launch command `PYTHONPATH=src streamlit run src/balance_pricing_engine/dashboard/app.py` (plain `streamlit run …` after `pip install -e ".[dashboard]"`), `PRICING_WATCH_CONFIG` and `PRICING_INTELLIGENCE_DB_PATH`, each of the seven pages (one paragraph each), **Refresh Dashboard reads stored state only / Run Pricing Watch performs live collection**, freshness semantics (observation age vs collector last success; threshold = `max_observation_age_hours`, default 168 h; stale records are shown marked but excluded from rankings/statistics), scenario semantics (what "No-change optimum", "Match median/leader", "Model optimum = MARKET_MEDIAN row" mean; current rate is positioning input only), decision-support limitation, operational controls (dry-run effects, backup location `data/processed/pricing_intelligence/backups`, Telegram test), known limitations (no scheduler state; per-bank record counts not in the run summary; event ranks "—" when the store cannot reconstruct a run), troubleshooting (empty page → no DB; "database unavailable" → check path/permissions; "already running" → another collection holds the lock; stale banks → System Health). `README.md`: replace the dashboard row text/commands. `docs/architecture.md`: add `dashboard/` and `pricing_intelligence/health.py` to the module map. `AGENTS.md` "When making changes": add — *Dashboard pages render only: no freshness, ranking, bps, market-state or scenario economics in the UI (call `health.py`/`service`/`strategy`); reads never import `dashboard.actions`; a rerender must never start a collection.* `CHANGELOG.md`: entry.

- [ ] **Step 4: Leftover/placeholder sweep**

Run: `grep -rn "Coming in a later task\|TODO\|TBD" src/balance_pricing_engine/dashboard src/balance_pricing_engine/pricing_intelligence/health.py` → Expected: no output.

- [ ] **Step 5: Request review** — invoke `superpowers:requesting-code-review` over `git diff master...HEAD`, focusing on: duplicated backend logic, wrong pricing/units (percent vs fraction), unsafe operational controls, Streamlit rerun problems, stale-data handling, caching of health, test gaps, over-engineering. Process findings with `superpowers:receiving-code-review`; fix confirmed ones via `systematic-debugging` + a regression test each.

- [ ] **Step 6: Verification (`superpowers:verification-before-completion`)** — fresh output for each, in this order:

```bash
python3 -m pytest -q -m 'not live_rates'                     # full suite (baseline 1603 passed, 2 skipped, + new)
python3 -m pytest -q tests/unit/test_dashboard_*.py tests/unit/test_pricing_health.py
python3 -m ruff check .
python3 -m ruff format --check src/balance_pricing_engine/dashboard src/balance_pricing_engine/pricing_intelligence/health.py tests/unit/test_dashboard_*.py tests/unit/test_pricing_health.py tests/unit/conftest.py tests/unit/dashboard_harness.py
python3 -m mypy --ignore-missing-imports src/balance_pricing_engine/dashboard src/balance_pricing_engine/pricing_intelligence/health.py
# smoke: real server boots and health-checks, then stops
PYTHONPATH=src python3 -m streamlit run src/balance_pricing_engine/dashboard/app.py --server.headless true --server.port 8765 &
sleep 8; curl -fsS localhost:8765/_stcore/health; kill %1
git diff master...HEAD --stat; git status --short
```

Report exact counts. Format/mypy findings in **pre-existing** files are out of scope (state this).

- [ ] **Step 7: Finish** — invoke `superpowers:finishing-a-development-branch`: present options, do not merge; prepare PR text (summary, test evidence, limitations, next three improvements). Final report per the task brief.

**Acceptance:** all brief acceptance criteria demonstrably green; five flows A–E encoded as tests; independent review done; branch ready for PR, not merged.

---

## Self-Review (completed before handoff)

**Spec coverage:** §1 invariants → Task 10 boundary tests + Task 8 safety tests; §3 gaps → each handled in the owning task (scheduler: Task 9 last-run row; event ranks: Task 5; exclusions: Task 1; manual rate: Task 7); §5.1 health → Task 1; §5.2 event context → Task 5; §6 services → Tasks 2,5,6,7,9; §7 pages 1–7 → Tasks 3,4,5,6,7,8,9; §8 operations/state machine → Task 8 (note: the spec's session-state flag is implemented as a process-level single-flight `JobRunner`, which also covers two browser tabs — a deliberate strengthening); §9 caching → Task 3 (`cached`, never for health); §10–12 → Tasks 2,3,9; §13 testing → throughout, flows in Task 10; §14 docs → Task 10; §15 decomposition → Tasks 1–10 (merged T3's "router" with Overview and moved the old-test deletion there).

**Known deviations from the spec (flagged, not hidden):** (1) services open a normal `ObservationStore` guarded by an existence check instead of `read_only=True` — read-only mode copies the whole database per call; the existence guard prevents file creation (pinned by a test). (2) Per-bank record counts after a live run are not in the workflow summary; the Operations result shows bank status plus totals, and System Health shows current record counts. (3) Product/rate-type filters on Market Watch are absent: the backend defines one comparable tier per product.

**Placeholder scan:** the only intentional temporary stubs are the five page modules created in Task 3 and replaced in Tasks 4–9; Task 10 Step 4 greps for them. Two spots deliberately instruct the executor to open `git show master:…` for the exact old-test setup to port (history-withdrawal scenario, term-deposit section, scenario-lab fakes) rather than paraphrase it.

**Type consistency check:** `Result[T]`, `ChangeWindow/ChangeFilters` (frozen → hashable for `cached`), `ScenarioRequest` field names (`manual_rate_pct`, `current_rate_pct`), `DashboardServices` method names (`overview, term_summary, bank_positions, changes, event_detail, history, scenarios, health, runs, run_detail, suspicious, db_stamp, header`), `actions` names (`run_live, run_dry, create_backup, send_test_notification, JobRunner, default_runner, ActionResult, BankOutcome`) are used identically across tasks. Rates: percent for snapshot-derived models, fractions for `ScenarioRowView`/`SelectedScenario`.

**Execution risk register:** `AppTest` + `st.navigation` (Task 3 spike, fallback = sidebar radio); `st.fragment(run_every=…)` under `AppTest` (Task 8 note); `get_market_snapshot(as_of_run_id=…)` inclusive-of-run semantics for event ranks (Task 5 tests will reveal; debug, don't weaken); fixture calls to `record_observations`/`detect_changes`/`update_collector_health` were written from source reading and have not been executed (Task 1 Step 3).
