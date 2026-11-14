"""Thin read-model over the pricing-intelligence service.

Every number shown in the dashboard comes from here, and every number here comes
from a backend call. No rankings, bps, freshness or market statistics are derived
in this module -- see tests/unit/test_dashboard_boundaries.py.

Every market read takes ``product_type`` (default ``HOME_LOAN``, so existing callers are
unchanged) and passes it to *every* backend call it makes: the snapshot, exclusions,
quarantine, activity, changes, history and health. Mortgages rank lowest-first and deposits
highest-first in the backend; a read that forgot the product on one call would silently mix
markets or invert a ranking. Pages pass the product positionally through ``cached`` so it is
part of the cache identity.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, TypeVar

import yaml

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.dashboard.formatting import NZ_TZ
from balance_pricing_engine.dashboard.models import (
    SCENARIO_HOME_LOAN,
    SCENARIO_JOINT,
    SCENARIO_MODES,
    SCENARIO_TERM_DEPOSIT,
    BankPosition,
    ChangeFilters,
    ChangeRow,
    ChangeWindow,
    CurvePoint,
    EventDetail,
    HistoryData,
    HistoryPoint,
    MarketPoint,
    TimelineEvent,
    PositionView,
    ScenarioRequest,
    ScenarioRowView,
    ScenarioView,
    SelectedScenario,
    HeaderInfo,
    MarketMatrix,
    MatrixCell,
    Overview,
    RankedBank,
    RunDetail,
    RunRow,
    SuspiciousRow,
    TermSummary,
)
from balance_pricing_engine.dashboard.results import Result
from balance_pricing_engine.dashboard.settings import DashboardSettings
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.health import (
    FreshnessState,
    MarketExclusion,
    SystemHealth,
    assess_system_health,
    is_stale,
)
from balance_pricing_engine.pricing_intelligence.changes import RateChange
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.snapshot import (
    MarketSnapshot,
    TermMarket,
    term_market_from_rates,
)
from balance_pricing_engine.pricing_intelligence.store import ObservationStore, RunRecord
from balance_pricing_engine.pricing_intelligence.strategy import (
    CompetitorRateStrategy,
    MarketStrategyError,
    apply_manual_competitor_rate_to_config,
    apply_market_strategy_to_config,
)
from balance_pricing_engine.pricing_scenario_trigger import run_triggered_scenarios
from balance_pricing_engine.scenario_reporting import diagnostic_metadata, strategy_label
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

logger = logging.getLogger(__name__)
T = TypeVar("T")

NO_DATA_MESSAGE = (
    "No market data collected yet. Open Operations and run Pricing Watch, or run the "
    "collector from the CLI (see docs/pricing_intelligence.md)."
)
NO_DEPOSIT_DATA_MESSAGE = (
    "No term-deposit market data collected yet. Deposit rates are collected separately from "
    "mortgages: run `python3 scripts/collect_market_rates_workflow.py --product term_deposit "
    "--render-javascript` (see docs/pricing_intelligence.md). Mortgage rates are never "
    "substituted."
)


def no_data_message(product_type: ProductType | str = ProductType.HOME_LOAN) -> str:
    if ProductType(product_type) is ProductType.TERM_DEPOSIT:
        return NO_DEPOSIT_DATA_MESSAGE
    return NO_DATA_MESSAGE


def _no_term_message(term_months: int, product_type: ProductType | str) -> str:
    noun = "term-deposit" if ProductType(product_type) is ProductType.TERM_DEPOSIT else "mortgage"
    return f"No comparable {noun} market data at {term_months} months."
DB_UNAVAILABLE_MESSAGE = (
    "Market database unavailable. Stored data cannot be read right now; "
    "see System Health and the application log."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _last_update(
    service: PricingIntelligenceService, product_type: ProductType = ProductType.HOME_LOAN
) -> Optional[datetime]:
    """Completion time of the product's newest run that stored data (SUCCESS or PARTIAL_SUCCESS).

    A FAILED run collected nothing, so presenting its time as "last successful update" would
    suggest the data is fresher than it is. Each product has its own runs: a fresh mortgage
    collection says nothing about deposit freshness.
    """
    for run in service.store.get_recent_run_records(product_type=product_type, limit=50):
        if run.status != "FAILED":
            return run.completed_at
    return None


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

    def config_fingerprint(self) -> str:
        """Content provenance for the exact loaded YAML used by ``scenarios``."""
        return hashlib.sha256(self.settings.engine_config_text.encode("utf-8")).hexdigest()

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

    def _guard(
        self,
        fn: Callable[[PricingIntelligenceService], Result[T]],
        product_type: ProductType = ProductType.HOME_LOAN,
    ) -> Result[T]:
        try:
            with self._service() as service:
                return fn(service)
        except _MissingDatabase:
            return Result.empty(no_data_message(product_type))
        except (sqlite3.Error, OSError):
            logger.exception("market database read failed")
            return Result.error(DB_UNAVAILABLE_MESSAGE)
        except Exception:  # noqa: BLE001 - last resort: log detail, show one sentence
            logger.exception("unexpected dashboard service failure")
            return Result.error("Something went wrong reading market data; see the log.")

    # -------------------------------------------------------------- reads

    def header(self, product_type: ProductType = ProductType.HOME_LOAN) -> Result[HeaderInfo]:
        product = ProductType(product_type)

        def build(service: PricingIntelligenceService) -> Result[HeaderInfo]:
            now = self._clock()
            return Result.ok(_header_info(service, product, now)[0])

        return self._guard(build, product)

    def overview(self, product_type: ProductType = ProductType.HOME_LOAN) -> Result[Overview]:
        product = ProductType(product_type)

        def build(service: PricingIntelligenceService) -> Result[Overview]:
            now = self._clock()
            snapshot = service.get_market_snapshot(product_type=product, now=now)
            exclusions = tuple(service.get_market_exclusions(product_type=product, now=now))
            if not snapshot.terms and not exclusions:
                return Result.empty(no_data_message(product))
            local_midnight = now.astimezone(NZ_TZ).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            # Every figure in the activity strip comes from the backend's own activity
            # computation, which excludes suspicious and non-comparable moves, so the counts
            # always agree with the market-state rationale shown beneath them.
            today_activity = service.get_market_activity(
                since=local_midnight.astimezone(timezone.utc), now=now, product_type=product
            )
            week_activity = service.get_market_activity(days=7, now=now, product_type=product)
            moves_by_term: dict[int, int] = {}
            for change in service.get_changes(
                days=7,
                now=now,
                scope_to_market=True,
                product_type=product,
                rate_category=snapshot.rate_category,
            ):
                if change.is_rate_move and not change.suspicious and change.term_months:
                    moves_by_term[change.term_months] = moves_by_term.get(change.term_months, 0) + 1
            terms = tuple(sorted(set(snapshot.term_months) | {e.term_months for e in exclusions}))
            selectable_terms = snapshot.term_months
            quarantined = service.get_market_quarantined(product, snapshot=snapshot)
            cells: dict[tuple[str, int], MatrixCell] = {}
            for term in snapshot.term_months:
                for entry in snapshot[term].entries:
                    cells[(entry.bank, term)] = MatrixCell(
                        rate_pct=entry.rate_pct,
                        is_leader=entry.rank == 1,
                        rate_category=entry.rate_category,
                        quarantined=(entry.bank, term) in quarantined,
                    )
            for exclusion in exclusions:
                cells.setdefault(
                    (exclusion.bank, exclusion.term_months),
                    MatrixCell(rate_pct=exclusion.rate_pct, stale=True),
                )
            header, rationale = _header_info(service, product, now)
            return Result.ok(
                Overview(
                    header=header,
                    changes_today=today_activity.changes,
                    changes_7d=week_activity.changes,
                    banks_repricing_7d=week_activity.banks_changed,
                    dominant_direction=week_activity.dominant_direction,
                    most_active_term=(
                        max(moves_by_term, key=lambda t: (moves_by_term[t], -t))
                        if moves_by_term
                        else None
                    ),
                    market_state_rationale=rationale,
                    matrix=MarketMatrix(
                        banks=tuple(service.config.selected_banks), terms=terms, cells=cells
                    ),
                    selectable_terms=selectable_terms,
                    default_term=(
                        12
                        if 12 in selectable_terms
                        else (selectable_terms[0] if selectable_terms else None)
                    ),
                    exclusions=exclusions,
                    product_type=product,
                )
            )

        return self._guard(build, product)

    def term_summary(
        self, term_months: int, product_type: ProductType = ProductType.HOME_LOAN
    ) -> Result[TermSummary]:
        product = ProductType(product_type)

        def build(service: PricingIntelligenceService) -> Result[TermSummary]:
            now = self._clock()
            market = service.get_market_snapshot(product_type=product, now=now).term(term_months)
            if market is None:
                return Result.empty(_no_term_message(term_months, product))
            return Result.ok(
                TermSummary(
                    term_months=term_months,
                    leaders=market.market_leaders,
                    lowest_pct=market.lowest_rate,
                    median_pct=market.median_rate,
                    highest_pct=market.highest_rate,
                    spread_bps=market.spread_bps,
                    banks_observed=market.bank_count,
                    # Live snapshots already apply shared freshness rules and deduplicate
                    # eligible banks for this term, including trusted quarantine fallbacks.
                    fresh_banks=market.bank_count,
                    ranking=tuple(
                        RankedBank(e.rank, e.bank, e.rate_pct, e.product_name)
                        for e in market.ranking()
                    ),
                    best_pct=market.best_rate,
                    product_type=product,
                )
            )

        return self._guard(build, product)

    def bank_positions(
        self, term_months: int, product_type: ProductType = ProductType.HOME_LOAN
    ) -> Result[tuple[BankPosition, ...]]:
        product = ProductType(product_type)

        def build(service: PricingIntelligenceService) -> Result[tuple[BankPosition, ...]]:
            now = self._clock()
            snapshot = service.get_market_snapshot(product_type=product, now=now)
            market = snapshot.term(term_months)
            if market is None:
                return Result.empty(_no_term_message(term_months, product))
            quarantined = service.get_market_quarantined(product, snapshot=snapshot)
            last_change: dict[str, datetime] = {}
            for change in service.get_changes(
                days=None,
                term_months=term_months,
                now=now,
                scope_to_market=True,
                product_type=product,
                rate_category=snapshot.rate_category,
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
                            FreshnessState.STALE
                            if is_stale(
                                e.observed_at, now, service.config.max_observation_age_hours
                            )
                            else FreshnessState.FRESH
                        ),
                        quarantined=(e.bank, term_months) in quarantined,
                    )
                    for e in market.ranking()
                )
            )

        return self._guard(build, product)

    # ------------------------------------------------------------- history

    def history(
        self,
        term_months: int,
        banks: tuple[str, ...],
        days: Optional[int],
        product_type: ProductType = ProductType.HOME_LOAN,
    ) -> Result[HistoryData]:
        """Per-bank rate history, market median/leader/spread per date, repricing timeline.

        ``banks`` filters the lines only; market statistics always use every bank
        observed on that date (via ``term_market_from_rates``, the snapshot's own maths,
        given the product so a deposit leader is the *highest* rate on each date).
        """
        product = ProductType(product_type)

        def build(service: PricingIntelligenceService) -> Result[HistoryData]:
            now = self._clock()
            local_now = now.astimezone(NZ_TZ)
            rate_category = service.get_market_snapshot(product_type=product, now=now).rate_category
            series = service.get_rate_history_series(
                term_months=term_months, rate_category=rate_category, product_type=product, tz=NZ_TZ
            )
            if days is not None:
                cutoff = (local_now - timedelta(days=days)).date()
                before = [entry for entry in series if entry[0] < cutoff]
                series = [entry for entry in series if entry[0] >= cutoff]
                if before and (not series or series[0][0] > cutoff):
                    # The rates in force when the window opens: a quiet market has no change
                    # inside the window, but it still has a steady rate to show.
                    series.insert(0, (cutoff, before[-1][1]))
            if series and series[-1][0] < local_now.date():
                series.append((local_now.date(), series[-1][1]))  # rates hold until they change
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
                term_market = term_market_from_rates(term_months, rates, product_type=product)
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
                        days=days,
                        term_months=term_months,
                        now=now,
                        scope_to_market=True,
                        product_type=product,
                        rate_category=rate_category,
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
            return Result.ok(HistoryData(points, tuple(market), timeline, product))

        return self._guard(build, product)

    # -------------------------------------------------------------- health

    def health(self, product_type: ProductType = ProductType.HOME_LOAN) -> Result[SystemHealth]:
        """The backend's own health model, evaluated now. Never cache this."""

        def build(service: PricingIntelligenceService) -> Result[SystemHealth]:
            return Result.ok(
                assess_system_health(service, product_type=product_type, now=self._clock())
            )

        return self._guard(build, product_type)

    def exclusions(
        self, product_type: ProductType = ProductType.HOME_LOAN
    ) -> Result[tuple[MarketExclusion, ...]]:
        """Records the market snapshot dropped for staleness, and why. Never cache this."""

        def build(service: PricingIntelligenceService) -> Result[tuple[MarketExclusion, ...]]:
            found = service.get_market_exclusions(product_type=product_type, now=self._clock())
            return Result.ok(tuple(found))

        return self._guard(build)

    def runs(
        self, product_type: ProductType = ProductType.HOME_LOAN, limit: int = 20
    ) -> Result[tuple[RunRow, ...]]:
        def build(service: PricingIntelligenceService) -> Result[tuple[RunRow, ...]]:
            records = service.store.get_recent_run_records(product_type=product_type, limit=limit)
            if not records:
                return Result.empty(
                    "No operational runs recorded yet. Run Pricing Watch from Operations."
                )
            return Result.ok(tuple(_run_row(r) for r in records))

        return self._guard(build)

    def run_detail(self, run_id: str) -> Result[RunDetail]:
        def build(service: PricingIntelligenceService) -> Result[RunDetail]:
            record = service.store.get_run_record(run_id)
            if record is None:
                return Result.empty("That run is no longer in the stored history.")
            return Result.ok(
                RunDetail(
                    row=_run_row(record),
                    banks_successful=tuple(record.banks_successful),
                    banks_failed=tuple(record.banks_failed),
                    validation_failures=record.validation_failures,
                    suspicious=record.suspicious_observations,
                    market_state=record.market_state,
                    error_summary=record.error_summary,
                )
            )

        return self._guard(build)

    def suspicious(
        self, product_type: ProductType = ProductType.HOME_LOAN
    ) -> Result[tuple[SuspiciousRow, ...]]:
        """Implausible rate moves, kept apart from trusted data (and from Rate Changes)."""

        def build(service: PricingIntelligenceService) -> Result[tuple[SuspiciousRow, ...]]:
            changes = service.get_changes(
                days=None, now=self._clock(), scope_to_market=True, product_type=product_type
            )
            rows = tuple(
                SuspiciousRow(
                    c.bank,
                    c.term_months,
                    c.previous_rate_pct,
                    c.current_rate_pct,
                    c.change_bps,
                    c.detected_at,
                )
                for c in changes
                if c.suspicious and c.is_rate_move
            )
            if not rows:
                return Result.empty("No suspicious observations recorded.")
            return Result.ok(rows)

        return self._guard(build)

    # ----------------------------------------------------------- scenarios

    def scenarios(self, request: ScenarioRequest) -> Result[ScenarioView]:
        """Run the optimiser's scenario set plus the operator's selected assumption.

        Decision support only: nothing here prices anything. ``request.mode`` picks the side
        whose competitor benchmark is overlaid, through the same seams the scheduled workflow
        uses: ``run_triggered_scenarios`` with a home-loan *or* a term-deposit snapshot, and
        ``apply_market_strategy_to_config`` with its loan or ``deposit_*`` arguments (both for
        the joint mode). The optimiser always solves loan and deposit rates jointly; a mode only
        decides which competitor input moves and which output is the scenario's own rate.
        """
        mode = request.mode
        if mode not in SCENARIO_MODES:
            return Result.error(f"Unknown scenario mode: {mode}.")

        def build(service: PricingIntelligenceService) -> Result[ScenarioView]:
            now = self._clock()
            try:
                base = EngineConfig(**yaml.safe_load(self.settings.engine_config_text))
            except Exception:  # noqa: BLE001 - any unreadable config is one operational message
                logger.exception("engine config unreadable")
                return Result.error(
                    "The engine config could not be read; check PRICING_WATCH_CONFIG."
                )
            loan_snapshot = deposit_snapshot = deposit_term = None
            if mode in (SCENARIO_HOME_LOAN, SCENARIO_JOINT):
                loan_snapshot = service.get_market_snapshot(now=now)
                if request.term_months not in loan_snapshot.term_months:
                    return Result.empty(
                        _no_term_message(request.term_months, ProductType.HOME_LOAN)
                    )
            if mode in (SCENARIO_TERM_DEPOSIT, SCENARIO_JOINT):
                deposit_snapshot = service.get_market_snapshot(
                    product_type=ProductType.TERM_DEPOSIT, now=now
                )
                deposit_term = (
                    request.deposit_term_months if mode == SCENARIO_JOINT else request.term_months
                )
                if deposit_term not in deposit_snapshot.term_months:
                    return Result.empty(_no_term_message(deposit_term, ProductType.TERM_DEPOSIT))
            strategy = CompetitorRateStrategy(request.strategy)
            try:
                comparison, selected_config = self._scenario_configs(
                    base, request, strategy, loan_snapshot, deposit_snapshot
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
            is_deposit = mode == SCENARIO_TERM_DEPOSIT
            side_product = ProductType.TERM_DEPOSIT if is_deposit else ProductType.HOME_LOAN
            position = None
            if request.current_rate_pct is not None and mode != SCENARIO_JOINT:
                try:
                    pos = service.get_competitive_position(
                        request.current_rate_pct,
                        term_months=request.term_months,
                        now=now,
                        product_type=side_product,
                    )
                except ValueError:
                    pos = None
                if pos is not None:
                    position = PositionView(
                        pos.market_rank,
                        pos.our_rate_minus_best_bps,
                        pos.our_rate_minus_median_bps,
                        pos.market_best,
                        pos.market_median,
                        pos.bank_count,
                    )
            optimum = comparison.median_reference if comparison else None
            market = selected_config.market
            loan_exclusions = deposit_exclusions = ()
            if loan_snapshot is not None:
                loan_exclusions = tuple(
                    e
                    for e in service.get_market_exclusions(now=now)
                    if e.term_months == request.term_months
                )
            if deposit_snapshot is not None:
                deposit_exclusions = tuple(
                    e
                    for e in service.get_market_exclusions(
                        product_type=ProductType.TERM_DEPOSIT, now=now
                    )
                    if e.term_months == deposit_term
                )
            return Result.ok(
                ScenarioView(
                    request=request,
                    rows=tuple(
                        ScenarioRowView(**asdict(r))
                        for r in (comparison.rows if comparison else ())
                    ),
                    selected=SelectedScenario(
                        our_rate=best.deposit_rate if is_deposit else best.loan_rate,
                        objective=best.total_objective,
                        competitor_rate=(
                            market.competitor_deposit_rate
                            if is_deposit
                            else market.competitor_loan_rate
                        ),
                        rationale=_rationale(strategy, request),
                        **diagnostic_metadata(diagnostics),
                        mode=mode,
                        loan_rate=best.loan_rate,
                        deposit_rate=best.deposit_rate,
                        deposit_interest_cost=best.deposit_interest_cost,
                        loan_competitor_rate=market.competitor_loan_rate,
                        deposit_competitor_rate=market.competitor_deposit_rate,
                    ),
                    curve=_objective_curve(diagnostics.grid_results, "loan_rate"),
                    position=position,
                    model_optimum_rate=optimum.our_rate if optimum else None,
                    excluded_banks=deposit_exclusions if is_deposit else loan_exclusions,
                    mode=mode,
                    deposit_curve=_objective_curve(diagnostics.grid_results, "deposit_rate"),
                    deposit_excluded_banks=deposit_exclusions if mode == SCENARIO_JOINT else (),
                )
            )

        return self._guard(build)

    @staticmethod
    def _scenario_configs(base, request, strategy, loan_snapshot, deposit_snapshot):
        """(comparison set or None, the selected scenario's config) for the request's mode."""
        if request.mode == SCENARIO_JOINT:
            deposit_strategy = CompetitorRateStrategy(request.deposit_strategy or request.strategy)
            if CompetitorRateStrategy.MANUAL in (strategy, deposit_strategy):
                raise MarketStrategyError(
                    "the joint scenario needs a market benchmark on both sides"
                )
            # No comparison set: run_triggered_scenarios varies one side at a time.
            return None, apply_market_strategy_to_config(
                base,
                strategy=strategy,
                snapshot=loan_snapshot,
                term_months=request.term_months,
                offset_bps=request.offset_bps,
                deposit_snapshot=deposit_snapshot,
                deposit_strategy=deposit_strategy,
                deposit_term_months=request.deposit_term_months,
                deposit_offset_bps=request.deposit_offset_bps,
            )
        is_deposit = request.mode == SCENARIO_TERM_DEPOSIT
        snapshot = deposit_snapshot if is_deposit else loan_snapshot
        comparison = run_triggered_scenarios(
            base, term_months=request.term_months, snapshot=snapshot
        )
        if strategy is CompetitorRateStrategy.MANUAL:
            if request.manual_rate_pct is None:
                raise MarketStrategyError("MANUAL needs a manual competitor rate.")
            return comparison, apply_manual_competitor_rate_to_config(
                base,
                request.manual_rate_pct / 100,
                product_type=snapshot.product_type,
            )
        if is_deposit:
            return comparison, apply_market_strategy_to_config(
                base,
                deposit_strategy=strategy,
                deposit_snapshot=snapshot,
                deposit_term_months=request.term_months,
                deposit_offset_bps=request.offset_bps,
            )
        return comparison, apply_market_strategy_to_config(
            base,
            strategy=strategy,
            snapshot=snapshot,
            term_months=request.term_months,
            offset_bps=request.offset_bps,
        )

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
        self,
        window: ChangeWindow,
        filters: Optional[ChangeFilters] = None,
        product_type: ProductType = ProductType.HOME_LOAN,
    ) -> Result[tuple[ChangeRow, ...]]:
        """One product's events. Mortgage and deposit events are never mixed in one read."""
        filters = filters or ChangeFilters()
        product = ProductType(product_type)

        def build(service: PricingIntelligenceService) -> Result[tuple[ChangeRow, ...]]:
            now = self._clock()
            changes = service.get_changes(
                now=now,
                scope_to_market=True,
                product_type=product,
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

        return self._guard(build, product)

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
            rate_category=change.rate_category,
            product_type=ProductType(change.product_type),
        )

    def event_detail(
        self, key: str, product_type: ProductType = ProductType.HOME_LOAN
    ) -> Result[EventDetail]:
        """Provenance for one event, looked up in its own product's history only."""
        product = ProductType(product_type)

        def build(service: PricingIntelligenceService) -> Result[EventDetail]:
            now = self._clock()
            change = next(
                (
                    c
                    for c in service.get_changes(
                        days=None, now=now, scope_to_market=True, product_type=product
                    )
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
                product_type=change.product_type,
                rate_category=change.rate_category,
                days=None,
                now=change.detected_at,
            )
            record = next(
                (
                    r
                    for r in reversed(history)
                    if r.observation_key() == change.observation_key
                    and r.captured_at_utc <= change.detected_at
                ),
                None,
            )
            market = ranks.new_market(change)
            state = (
                service.get_market_state(now=change.detected_at, product_type=change.product_type)
                if market
                else None
            )
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
                    market_state=state.state.value if state else None,
                )
            )

        return self._guard(build, product)


def _header_info(
    service: PricingIntelligenceService, product: ProductType, now: datetime
) -> tuple[HeaderInfo, str]:
    """One product's header and its market-state rationale, from that product's data only."""
    health = assess_system_health(service, product_type=product, now=now)
    state = service.get_market_state(now=now, product_type=product)
    header = HeaderInfo(
        last_update=_last_update(service, product),
        market_state=state.state.value,
        fresh_banks=health.fresh_bank_count,
        configured_banks=len(health.banks),
        product_type=product,
    )
    return header, state.rationale


def _run_row(record: RunRecord) -> RunRow:
    return RunRow(
        run_id=record.run_id,
        started_at=record.started_at,
        status=record.status,
        banks_ok=len(record.banks_successful),
        banks_failed=len(record.banks_failed),
        observations=record.rates_observed,
        changes=record.rates_changed,
        duration_seconds=(record.completed_at - record.started_at).total_seconds(),
    )


def _rationale(strategy: CompetitorRateStrategy, request: ScenarioRequest) -> str:
    side = {SCENARIO_TERM_DEPOSIT: "term deposit", SCENARIO_JOINT: "home loan"}.get(
        request.mode, "home loan"
    )
    if strategy is CompetitorRateStrategy.MANUAL:
        return f"{side} manual competitor rate {request.manual_rate_pct:.2f}%"
    offset = f", offset {request.offset_bps:+.0f} bps" if request.offset_bps else ""
    product = (
        ProductType.TERM_DEPOSIT if request.mode == SCENARIO_TERM_DEPOSIT else ProductType.HOME_LOAN
    )
    text = (
        f"{side} {strategy_label(strategy.value, product)} at {request.term_months} months{offset}"
    )
    if request.mode == SCENARIO_JOINT:
        deposit = strategy_label(
            request.deposit_strategy or strategy.value, ProductType.TERM_DEPOSIT
        )
        deposit_offset = (
            f", offset {request.deposit_offset_bps:+.0f} bps" if request.deposit_offset_bps else ""
        )
        text += f"; term deposit {deposit} at {request.deposit_term_months} months{deposit_offset}"
    return text


def _objective_curve(grid_results, rate_column: str) -> tuple[CurvePoint, ...]:
    """A view of the backend grid: best objective at each candidate rate on one side, across
    every rate on the other. For charting only; no economics are computed here."""
    return tuple(
        CurvePoint(float(rate), float(objective))
        for rate, objective in grid_results.groupby(rate_column)["total_objective"].max().items()
    )


def _event_key(change: RateChange) -> str:
    return (
        f"{change.observation_key}|{change.change_type.value}|"
        f"{change.detected_at.isoformat()}|{change.run_id}"
    )


class _RankCache:
    """Resolve comparable-market context before/after an event, cached per product/run."""

    def __init__(self, service: PricingIntelligenceService) -> None:
        self._service = service
        self._snapshots: dict[tuple[ProductType, str], MarketSnapshot] = {}

    def _snapshot(self, run_id: Optional[str], product: ProductType) -> Optional[MarketSnapshot]:
        if run_id is None:
            return None
        key = (product, run_id)
        if key not in self._snapshots:
            self._snapshots[key] = self._service.get_market_snapshot(
                product_type=product, as_of_run_id=run_id
            )
        return self._snapshots[key]

    def _previous_run_id(self, run_id: str, product: ProductType) -> Optional[str]:
        previous = self._service.store.get_previous_run(before_run_id=run_id, product_type=product)
        return previous.run_id if previous else None

    def _market(self, change: RateChange, run_id: Optional[str]) -> Optional[TermMarket]:
        snapshot = self._snapshot(run_id, change.product_type)
        # Audit events remain visible, but cannot borrow context from another rate tier.
        if (
            snapshot is None
            or change.term_months is None
            or change.suspicious
            or change.rate_category == "non_comparable"
            or change.rate_category != snapshot.rate_category
        ):
            return None
        return snapshot.term(change.term_months)

    def new_market(self, change: RateChange) -> Optional[TermMarket]:
        return self._market(change, change.run_id)

    def for_change(self, change: RateChange) -> tuple[Optional[int], Optional[int]]:
        if not change.is_rate_move or change.run_id is None or change.term_months is None:
            return (None, None)
        new_market = self.new_market(change)
        previous_run = self._previous_run_id(change.run_id, change.product_type)
        previous_market = self._market(change, previous_run)
        return (
            previous_market.rank_for(change.bank) if previous_market else None,
            new_market.rank_for(change.bank) if new_market else None,
        )
