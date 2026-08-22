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

        if not persist or not validation.accepted:
            return CollectionOutcome(
                report=report, validation=validation, write_result=None, changes=()
            )

        write_result = self.store.record_observations(
            validation.accepted, observed_at=captured_at_utc
        )
        changes = detect_changes(
            previous=baseline,
            current=validation.accepted,
            detected_at=captured_at_utc,
            thresholds=self.config.validation,
            run_id=write_result.run_id,
            # Scoped to banks that actually responded, so an unreachable bank is
            # never reported as having withdrawn its whole range.
            banks_collected=self.succeeded_bank_names(report),
            # A record rejected by validation this run is unknown, not
            # withdrawn -- a single implausible line must not read as a
            # market event.
            rejected_keys=(record.observation_key() for record in validation.rejected),
        )
        # A first-ever collection would otherwise report every rate as a new
        # product, which is noise rather than market intelligence.
        if not baseline:
            logger.info("first collection for this store; suppressing new-product events")
            changes = []
        if changes:
            self.store.record_changes(changes)

        return CollectionOutcome(
            report=report,
            validation=validation,
            write_result=write_result,
            changes=tuple(changes),
        )

    @staticmethod
    def succeeded_bank_names(report: CollectionReport) -> tuple[str, ...]:
        """Banks whose pages were read and parsed successfully in a run."""
        return tuple(result.bank for result in report.succeeded)

    # ------------------------------------------------------------ market view

    def get_market_snapshot(
        self,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        rate_category: Optional[str] = "special",
        as_of_run_id: Optional[str] = None,
    ) -> MarketSnapshot:
        """Return the currently-advertised market across tracked terms."""
        records = self.store.get_latest_rates(
            product_type=product_type, run_id=as_of_run_id
        )
        return build_market_snapshot(
            records,
            product_type=product_type,
            rate_category=rate_category,
            terms_months=self.config.tracked_terms_months,
            banks=self.config.selected_banks,
        )

    def get_previous_market_snapshot(
        self,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        rate_category: Optional[str] = "special",
    ) -> Optional[MarketSnapshot]:
        """Return the snapshot from the run before the latest, if there is one."""
        records = self.store.get_previous_run_rates(product_type=product_type)
        if not records:
            return None
        return build_market_snapshot(
            records,
            product_type=product_type,
            rate_category=rate_category,
            terms_months=self.config.tracked_terms_months,
            banks=self.config.selected_banks,
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
    ) -> CompetitivePosition:
        """Position a rate (in advertised percent) against the current market."""
        snapshot = self.get_market_snapshot(rate_category=rate_category)
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
    ) -> ResolvedCompetitorAssumption:
        """Resolve the competitor assumption the optimiser should run with."""
        snapshot = self.get_market_snapshot(rate_category=rate_category)
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
