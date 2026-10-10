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
from balance_pricing_engine.dashboard.formatting import NZ_TZ
from balance_pricing_engine.dashboard.models import (
    BankPosition,
    ChangeFilters,
    ChangeRow,
    ChangeWindow,
    EventDetail,
    HistoryData,
    HistoryPoint,
    MarketPoint,
    TimelineEvent,
    HeaderInfo,
    MarketMatrix,
    MatrixCell,
    Overview,
    RankedBank,
    TermSummary,
)
from balance_pricing_engine.dashboard.results import Result
from balance_pricing_engine.dashboard.settings import DashboardSettings
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.health import (
    FreshnessState,
    assess_system_health,
)
from balance_pricing_engine.pricing_intelligence.changes import RateChange
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.snapshot import (
    MarketSnapshot,
    TermMarket,
    term_market_from_rates,
)
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
            local_midnight = now.astimezone(NZ_TZ).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            today = service.get_changes(
                days=None,
                since=local_midnight.astimezone(timezone.utc),
                now=now,
                scope_to_market=True,
            )
            week = service.get_changes(days=7, now=now, scope_to_market=True)
            moves_by_term: dict[int, int] = {}
            for change in week:
                if change.is_rate_move and not change.suspicious and change.term_months:
                    moves_by_term[change.term_months] = moves_by_term.get(change.term_months, 0) + 1
            terms = tuple(sorted(set(snapshot.term_months) | {e.term_months for e in exclusions}))
            cells: dict[tuple[str, int], MatrixCell] = {}
            for term in snapshot.term_months:
                for entry in snapshot[term].entries:
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
                        max(moves_by_term, key=lambda t: (moves_by_term[t], -t))
                        if moves_by_term
                        else None
                    ),
                    market_state_rationale=state.rationale,
                    matrix=MarketMatrix(
                        banks=tuple(service.config.selected_banks), terms=terms, cells=cells
                    ),
                    default_term=12 if 12 in terms else (terms[0] if terms else None),
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
                        freshness=(
                            health[e.bank].freshness.state
                            if e.bank in health
                            else FreshnessState.UNKNOWN
                        ),
                    )
                    for e in market.ranking()
                )
            )

        return self._guard(build)

    # ------------------------------------------------------------- history

    def history(
        self, term_months: int, banks: tuple[str, ...], days: Optional[int]
    ) -> Result[HistoryData]:
        """Per-bank rate history, market median/leader/spread per date, repricing timeline.

        ``banks`` filters the lines only; market statistics always use every bank
        observed on that date (via ``term_market_from_rates``, the snapshot's own maths).
        """

        def build(service: PricingIntelligenceService) -> Result[HistoryData]:
            now = self._clock()
            series = service.get_rate_history_series(term_months=term_months)
            if days is not None:
                cutoff = (now - timedelta(days=days)).date()
                series = [(day, rates) for day, rates in series if day >= cutoff]
            if not series:
                return Result.empty("No rate history for this term and range yet.")
            points = tuple(
                HistoryPoint(day, bank, rates[bank])
                for day, rates in series
                for bank in banks
                if bank in rates
            )
            market = []
            for day, rates in series:
                if not rates:
                    continue
                term_market = term_market_from_rates(term_months, rates)
                market.append(
                    MarketPoint(
                        day,
                        term_market.median_rate,
                        term_market.best_rate,
                        term_market.spread_bps,
                        term_market.market_leaders,
                    )
                )
            moves = sorted(
                (
                    c
                    for c in service.get_changes(
                        days=days, term_months=term_months, now=now, scope_to_market=True
                    )
                    if c.is_rate_move and not c.suspicious
                ),
                key=lambda c: c.detected_at,
            )
            first = moves[0].detected_at if moves else None
            timeline = tuple(
                TimelineEvent(
                    c.detected_at,
                    c.bank,
                    c.change_bps,
                    round((c.detected_at - first).total_seconds() / 86400, 1),
                )
                for c in moves
                if first is not None and c.change_bps is not None
            )
            return Result.ok(HistoryData(points, tuple(market), timeline))

        return self._guard(build)

    # -------------------------------------------------------------- changes

    @staticmethod
    def _window_args(window: ChangeWindow, now: datetime) -> dict:
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
                now=now,
                scope_to_market=True,
                bank=filters.bank,
                term_months=filters.term_months,
                **self._window_args(window, now),
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
    def _row(change: RateChange, ranks: "_RankCache") -> ChangeRow:
        previous_rank, new_rank = ranks.for_change(change)
        return ChangeRow(
            key=_event_key(change),
            detected_at=change.detected_at,
            bank=change.bank,
            term_months=change.term_months,
            previous_pct=change.previous_rate_pct,
            current_pct=change.current_rate_pct,
            change_bps=change.change_bps,
            direction=change.direction,
            change_type=change.change_type.value,
            previous_rank=previous_rank,
            new_rank=new_rank,
        )

    def event_detail(self, key: str) -> Result[EventDetail]:
        def build(service: PricingIntelligenceService) -> Result[EventDetail]:
            now = self._clock()
            change = next(
                (
                    c
                    for c in service.get_changes(days=None, now=now, scope_to_market=True)
                    if _event_key(c) == key
                ),
                None,
            )
            if change is None:
                return Result.empty("That event is no longer in the stored history.")
            ranks = _RankCache(service)
            history = service.get_rate_history(
                bank=change.bank,
                term_months=change.term_months,
                rate_category=change.rate_category,
                days=None,
                now=change.detected_at,
            )
            record = next(
                (
                    r
                    for r in reversed(history)
                    if r.product_name == change.product_name
                    and r.captured_at_utc <= change.detected_at
                ),
                None,
            )
            market = ranks.new_market(change)
            state = service.get_market_state(now=change.detected_at)
            return Result.ok(
                EventDetail(
                    row=self._row(change, ranks),
                    product_name=change.product_name,
                    rate_category=change.rate_category,
                    source_url=change.source_url or (record.source_url if record else None),
                    observed_at=record.captured_at_utc if record else None,
                    effective_date_text=record.effective_date_text if record else None,
                    parser_version=record.parser_version if record else None,
                    market_median_pct=market.median_rate if market else None,
                    market_state=state.state.value,
                )
            )

        return self._guard(build)


def _event_key(change: RateChange) -> str:
    return (
        f"{change.observation_key}|{change.change_type.value}|"
        f"{change.detected_at.isoformat()}|{change.run_id}"
    )


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

    def new_market(self, change: RateChange) -> Optional[TermMarket]:
        snapshot = self._snapshot(change.run_id)
        if snapshot is None or change.term_months is None:
            return None
        return snapshot.term(change.term_months)

    def for_change(self, change: RateChange) -> tuple[Optional[int], Optional[int]]:
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
