"""Thin read-model over the pricing-intelligence service.

Every number shown in the dashboard comes from here, and every number here comes
from a backend call. No rankings, bps, freshness or market statistics are derived
in this module -- see tests/unit/test_dashboard_boundaries.py.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from balance_pricing_engine.dashboard.formatting import NZ_TZ
from balance_pricing_engine.dashboard.models import (
    BankPosition,
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
