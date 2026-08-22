"""The pricing-intelligence facade.

One entry point for "what is happening in the NZ retail banking pricing
market?". Callers -- the CLI, the dashboard, scenario scripts -- use this rather
than wiring collectors, validation, storage, and change detection themselves.

It answers market questions only. It never prices anything: turning market
intelligence into a competitor assumption is
:mod:`balance_pricing_engine.pricing_intelligence.strategy`, and deciding a rate
remains the optimiser's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Optional, Sequence

from balance_pricing_engine.competitor_rates.collectors import (
    BankPricingCollector,
    CollectionReport,
    Fetcher,
    build_collectors,
    collect_all_mortgage_rates,
)
from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.changes import (
    RateChange,
    detect_changes,
    load_changes,
)
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.market_state import (
    MarketStateAssessment,
    classify_market_state,
)
from balance_pricing_engine.pricing_intelligence.positioning import (
    CompetitivePosition,
    MarketActivity,
    compute_competitive_position,
    compute_market_activity,
)
from balance_pricing_engine.pricing_intelligence.snapshot import (
    MarketSnapshot,
    build_market_snapshot,
)
from balance_pricing_engine.pricing_intelligence.store import ObservationStore, WriteResult
from balance_pricing_engine.pricing_intelligence.strategy import (
    CompetitorRateStrategy,
    ResolvedCompetitorAssumption,
    resolve_competitor_rate,
)
from balance_pricing_engine.pricing_intelligence.validation import (
    ValidationOutcome,
    validate_observations,
)

logger = logging.getLogger(__name__)


def _exclude_suspicious(
    records: Sequence[CompetitorRateRecord],
) -> list[CompetitorRateRecord]:
    """Drop records flagged suspicious pending human confirmation.

    A suspicious-but-accepted observation is still stored for audit and
    history, but must not feed a market-derived pricing view -- see
    :meth:`PricingIntelligenceService.collect`.
    """
    return [record for record in records if record.validation_status != "warning"]


def _exclude_stale(
    records: Sequence[CompetitorRateRecord], now: datetime, max_age_hours: float
) -> list[CompetitorRateRecord]:
    """Drop a record whose last confirmation is older than the freshness limit.

    Complements :func:`_exclude_suspicious`. An unreachable bank's last-known
    rate stays in change detection's baseline indefinitely (AGENTS.md: an
    unreachable bank is unknown, not withdrawn), but that same "unknown"
    value must not keep feeding a live market-derived pricing view forever --
    a months-stale rate could otherwise silently drive MARKET_LEADER or
    MARKET_MEDIAN.
    """
    cutoff = now - timedelta(hours=max_age_hours)
    return [record for record in records if record.captured_at_utc >= cutoff]


def _reliably_collected_banks(
    report: CollectionReport,
    accepted: Sequence[CompetitorRateRecord],
    baseline: Sequence[CompetitorRateRecord],
    min_coverage_ratio: float,
) -> tuple[str, ...]:
    """Banks whose collection this run plausibly covered their known range.

    A bank can report SUCCESS with just one parseable row after a page-layout
    regression silently drops the rest of its table. Passing that bank
    straight to withdrawal detection would then read every other, still
    genuinely-current product as withdrawn. A bank whose accepted-record
    count this run falls short of its previously known key count (below
    ``min_coverage_ratio``) is excluded here, same as a bank that failed to
    fetch outright: unknown, not withdrawn.
    """
    baseline_counts: dict[str, int] = {}
    for record in baseline:
        baseline_counts[record.bank] = baseline_counts.get(record.bank, 0) + 1
    accepted_counts: dict[str, int] = {}
    for record in accepted:
        accepted_counts[record.bank] = accepted_counts.get(record.bank, 0) + 1

    reliable: list[str] = []
    for bank in (result.bank for result in report.succeeded):
        known = baseline_counts.get(bank, 0)
        seen = accepted_counts.get(bank, 0)
        if known == 0 or seen >= known * min_coverage_ratio:
            reliable.append(bank)
    return tuple(reliable)


@dataclass(frozen=True)
class CollectionOutcome:
    """Everything one collection run produced."""

    report: CollectionReport
    validation: ValidationOutcome
    write_result: Optional[WriteResult]
    changes: tuple[RateChange, ...]

    @property
    def succeeded_banks(self) -> tuple[str, ...]:
        return tuple(result.bank for result in self.report.succeeded)

    @property
    def failed_banks(self) -> tuple[str, ...]:
        return tuple(result.bank for result in self.report.failed)

    @property
    def rate_count(self) -> int:
        return len(self.validation.accepted)

    def render(self) -> str:
        lines = [self.report.render(), ""]
        lines.append(f"Validation: {self.validation.summary()}")
        if self.write_result is not None:
            lines.append(
                f"Stored: {self.write_result.inserted} new advertised states, "
                f"{self.write_result.unchanged} unchanged"
            )
        lines.append(f"Changes detected: {len(self.changes)}")
        return "\n".join(lines)


class PricingIntelligenceService:
    """Collects, stores, and interprets public NZ bank pricing."""

    def __init__(
        self,
        config: Optional[PricingIntelligenceConfig] = None,
        store: Optional[ObservationStore] = None,
        collectors: Optional[Sequence[BankPricingCollector]] = None,
        fetcher: Optional[Fetcher] = None,
        raw_root: str | Path = "data/raw/competitor_rates",
        render_javascript: bool = False,
    ) -> None:
        self.config = config or PricingIntelligenceConfig()
        self.store = store or ObservationStore(self.config.database_path)
        self._collectors = collectors
        self._fetcher = fetcher
        self._raw_root = raw_root
        self.render_javascript = render_javascript

    # ------------------------------------------------------------- collection

    @property
    def collectors(self) -> list[BankPricingCollector]:
        if self._collectors is not None:
            return list(self._collectors)
        return build_collectors(
            banks=self.config.selected_banks,
            config=self.config.collection,
            fetcher=self._fetcher,
            raw_root=self._raw_root,
            render_javascript=self.render_javascript,
        )

    def collect(
        self,
        captured_at_utc: Optional[datetime] = None,
        persist: bool = True,
        persist_snapshot: bool = True,
    ) -> CollectionOutcome:
        """Run a full collection: fetch, validate, store, and detect changes.

        A failing bank is isolated into a FAILED entry in the report; the run
        continues and the remaining banks are still stored. Records failing
        validation are rejected before storage, so a parser fault cannot enter
        the history.
        """
        captured_at_utc = captured_at_utc or datetime.now(timezone.utc)
        report = collect_all_mortgage_rates(
            self.collectors,
            captured_at_utc=captured_at_utc,
            persist_snapshot=persist_snapshot,
            inter_request_delay_seconds=self.config.collection.inter_request_delay_seconds,
        )

        # Captured before this run's own storage creates a collection_runs
        # row: an empty baseline does not by itself mean "first collection
        # ever" (every currently tracked product could simply have been
        # withdrawn), so first-run suppression below needs this instead.
        is_first_collection = self.store.latest_run_id() is None

        # The last known state per key, not scoped to the latest completed
        # run: a bank that failed a prior run would otherwise fall out of the
        # baseline entirely, making its next (unchanged) collection look like
        # every advertised line is new.
        baseline = self.store.get_current_observations(product_type=ProductType.HOME_LOAN)
        validation = validate_observations(
            report.records, thresholds=self.config.validation, previous=baseline
        )
        for issue in validation.issues:
            logger.warning("validation: %s", issue)

        if not validation.accepted:
            return CollectionOutcome(
                report=report, validation=validation, write_result=None, changes=()
            )

        # A suspicious-but-accepted record (a plausible yet unusually large
        # move) is stored for audit and history, but tagged so market-derived
        # pricing views can exclude it until a human confirms it against the
        # source page -- a parser fault must not feed an unverified rate
        # straight into the optimiser.
        suspicious_keys = validation.suspicious_keys
        accepted = [
            record.model_copy(update={"validation_status": "warning"})
            if record.observation_key() in suspicious_keys
            else record
            for record in validation.accepted
        ]

        # A dry run (persist=False) skips writing, but still detects changes
        # against the stored baseline -- otherwise --no-persist could not be
        # used to preview what a real collection would do.
        write_result = (
            self.store.record_observations(accepted, observed_at=captured_at_utc)
            if persist
            else None
        )
        changes = detect_changes(
            previous=baseline,
            current=accepted,
            detected_at=captured_at_utc,
            thresholds=self.config.validation,
            run_id=write_result.run_id if write_result is not None else None,
            # Scoped to banks that both responded and whose coverage this run
            # plausibly matches their known range, so neither an unreachable
            # bank nor a partial-parse regression is read as withdrawing its
            # whole range.
            banks_collected=_reliably_collected_banks(
                report, accepted, baseline, self.config.validation.min_bank_coverage_ratio
            ),
            # A record rejected by validation this run is unknown, not
            # withdrawn -- a single implausible line must not read as a
            # market event.
            rejected_keys=(record.observation_key() for record in validation.rejected),
        )
        # A first-ever collection would otherwise report every rate as a new
        # product, which is noise rather than market intelligence.
        if is_first_collection:
            logger.info("first collection for this store; suppressing new-product events")
            changes = []
        if changes and persist:
            self.store.record_changes(changes)

        return CollectionOutcome(
            report=report,
            validation=validation,
            write_result=write_result,
            changes=tuple(changes),
        )

    # ------------------------------------------------------------ market view

    def get_market_snapshot(
        self,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        rate_category: Optional[str] = "special",
        as_of_run_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> MarketSnapshot:
        """Return the currently-advertised market across tracked terms.

        Uses the last known state per key (via ``get_current_observations()``),
        not a strict "last run" filter: a bank's rate that failed to fetch, or
        a single line another bank had rejected by validation, must not drop
        out of the market view just because it wasn't reconfirmed this exact
        run -- only a confirmed withdrawal does that. That last-known state is
        not kept forever, though: one older than
        ``config.max_observation_age_hours`` is dropped, so a bank gone quiet
        for weeks cannot keep driving MARKET_LEADER/MARKET_MEDIAN on a stale
        rate. Only applies to the live view -- an explicit ``as_of_run_id``
        reconstruction is exempt, since staleness relative to "now" is not
        the question being asked.

        ``as_of_run_id`` reconstructs an older run's state via
        ``get_observations_as_of()``: ``last_seen_run_id`` (what
        ``get_latest_rates()`` filters on) is overwritten forward on every
        unchanged re-observation, so it cannot answer for a run that isn't
        the latest one.
        """
        as_of: Optional[datetime] = None
        if as_of_run_id is not None:
            as_of = self.store.get_run_started_at(as_of_run_id)
            records = (
                self.store.get_observations_as_of(as_of=as_of, product_type=product_type)
                if as_of is not None
                else []
            )
        else:
            records = self.store.get_current_observations(product_type=product_type)
            records = _exclude_stale(
                records,
                now=now or datetime.now(timezone.utc),
                max_age_hours=self.config.max_observation_age_hours,
            )
        return build_market_snapshot(
            _exclude_suspicious(records),
            product_type=product_type,
            rate_category=rate_category,
            terms_months=self.config.tracked_terms_months,
            banks=self.config.selected_banks,
            as_of=as_of,
        )

    def get_previous_market_snapshot(
        self,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        rate_category: Optional[str] = "special",
    ) -> Optional[MarketSnapshot]:
        """Return the snapshot from the run before the latest, if there is one."""
        as_of = self.store.get_previous_run_started_at()
        if as_of is None:
            return None
        records = self.store.get_observations_as_of(as_of=as_of, product_type=product_type)
        if not records:
            return None
        return build_market_snapshot(
            _exclude_suspicious(records),
            product_type=product_type,
            rate_category=rate_category,
            terms_months=self.config.tracked_terms_months,
            banks=self.config.selected_banks,
            as_of=as_of,
        )

    def get_changes(
        self,
        days: int = 7,
        bank: Optional[str] = None,
        term_months: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> list[RateChange]:
        """Return detected changes inside a lookback window, newest first."""
        rows = self.store.get_changes_since(
            days=days, bank=bank, term_months=term_months, now=now
        )
        return load_changes(rows)

    def get_rate_history(
        self,
        bank: Optional[str] = None,
        term_months: Optional[int] = None,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        rate_category: Optional[str] = None,
        days: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> list[CompetitorRateRecord]:
        """Return the distinct advertised states for a bank/term, oldest first."""
        since = None
        if days is not None:
            since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
        return self.store.get_rate_history(
            bank=bank,
            term_months=term_months,
            product_type=product_type,
            rate_category=rate_category,
            since=since,
        )

    def get_market_activity(
        self, days: Optional[int] = None, now: Optional[datetime] = None
    ) -> MarketActivity:
        """Summarise repricing activity over a lookback window."""
        days = days or self.config.market_state.lookback_days
        return compute_market_activity(
            self.get_changes(days=days, now=now),
            lookback_days=days,
            now=now,
            directional_dominance_ratio=(
                self.config.market_state.directional_dominance_ratio
            ),
        )

    def get_market_state(self, now: Optional[datetime] = None) -> MarketStateAssessment:
        """Classify how competitively active the market currently is."""
        changes = self.get_changes(days=self.config.market_state.lookback_days, now=now)
        return classify_market_state(
            changes, thresholds=self.config.market_state, now=now
        )

    def get_competitive_position(
        self,
        our_rate: float,
        term_months: int = 12,
        rate_category: Optional[str] = "special",
        now: Optional[datetime] = None,
    ) -> CompetitivePosition:
        """Position a rate (in advertised percent) against the current market."""
        snapshot = self.get_market_snapshot(rate_category=rate_category, now=now)
        term_market = snapshot.term(term_months)
        if term_market is None:
            raise ValueError(
                f"no advertised market data at {term_months} months; "
                f"available terms: {list(snapshot.term_months)}"
            )
        return compute_competitive_position(
            term_market, our_rate, our_bank=self.config.our_bank
        )

    # ------------------------------------------------------- pricing hand-off

    def resolve_competitor_assumption(
        self,
        strategy: CompetitorRateStrategy | str = CompetitorRateStrategy.MARKET_MEDIAN,
        term_months: int = 12,
        manual_rate: Optional[float] = None,
        offset_bps: float = 0.0,
        rate_category: Optional[str] = "special",
        now: Optional[datetime] = None,
    ) -> ResolvedCompetitorAssumption:
        """Resolve the competitor assumption the optimiser should run with."""
        snapshot = self.get_market_snapshot(rate_category=rate_category, now=now)
        return resolve_competitor_rate(
            strategy=strategy,
            snapshot=snapshot,
            term_months=term_months,
            manual_rate=manual_rate,
            offset_bps=offset_bps,
        )

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "PricingIntelligenceService":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
