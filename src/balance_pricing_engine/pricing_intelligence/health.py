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
from typing import TYPE_CHECKING, Collection, Optional, Sequence

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


def comparable_records(
    records: Sequence[CompetitorRateRecord],
    *,
    rate_category: Optional[str],
    banks: Collection[str],
    terms: Collection[int],
    excluded_product_names: Collection[str],
) -> list[CompetitorRateRecord]:
    """Records eligible for the market snapshot, before the staleness check.

    The same filters ``build_market_snapshot`` applies (comparable tier, tracked bank and term,
    no ``non_comparable`` or name-excluded product). Exclusion and health logic must use this,
    not a looser record set, or they would describe records the market view never considered.
    """
    markers = tuple(name.lower() for name in excluded_product_names)
    return [
        r
        for r in records
        if r.rate_category != "non_comparable"
        and (rate_category is None or r.rate_category == rate_category)
        and r.bank in banks
        and r.term_months_normalised in terms
        and not any(marker in r.product_name.lower() for marker in markers)
    ]


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
    snapshot = service.get_market_snapshot(product_type=product, now=now)
    records = store.get_current_observations(product_type=product)
    # Freshness describes what the market view uses, not every row ever stored for the bank.
    comparable = comparable_records(
        records,
        rate_category=snapshot.rate_category,
        banks=set(config.selected_banks),
        terms=set(config.tracked_terms_months),
        excluded_product_names=config.excluded_product_names,
    )
    run_ids_by_bank = store.get_current_observation_run_ids(product_type=product)
    latest_run_id = store.latest_run_id(product_type=product)
    collectors = {c.bank: c for c in store.get_collector_health(product_type=product)}

    banks = []
    for bank in config.selected_banks:
        freshness = assess_observation_freshness(
            comparable,
            bank,
            now=now,
            max_age_hours=config.max_observation_age_hours,
            run_ids=run_ids_by_bank.get(bank, []),
            latest_run_id=latest_run_id,
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
