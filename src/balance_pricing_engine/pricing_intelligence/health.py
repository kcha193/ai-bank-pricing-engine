"""Single source of truth for freshness, collector health and market exclusions.

Before this module the same threshold logic lived in ``cli._health``,
``dashboard_data`` and ``service._exclude_stale``. Everything that needs to say
"fresh", "stale" or "excluded because stale" -- the CLI, the dashboard, the
market snapshot -- goes through here so they cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Collection, Optional, Sequence

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.alerts import (
    build_telegram_sink_from_env,
    build_webhook_sink_from_env,
)
from balance_pricing_engine.pricing_intelligence.quarantine import QuarantineStatus
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


class CollectionOutcome(str, Enum):
    """What the latest *attempted* collection did for one bank (``operational_runs``)."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    #: The latest run did not attempt this bank at all.
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    #: No operational run is recorded for the product (e.g. a legacy database).
    UNKNOWN = "UNKNOWN"


class OperationalState(str, Enum):
    """Observation freshness combined with the latest collection attempt (DATA-004)."""

    #: Fresh observations, and the bank was collected successfully in the latest attempt.
    FRESH = "FRESH"
    #: Usable observations remain, but the latest attempt failed, skipped the bank, or did not
    #: reconfirm every current observation.
    DEGRADED = "DEGRADED"
    #: The trusted observations are older than the freshness limit.
    STALE = "STALE"
    #: No trusted observation, or no collection evidence to judge it by.
    UNKNOWN = "UNKNOWN"
    #: A reading awaits operator review (Phase 2a).
    QUARANTINED = "QUARANTINED"


class RunHealth(str, Enum):
    """The product's latest operational run, independent of how old the rates are."""

    HEALTHY = "HEALTHY"  # SUCCESS
    DEGRADED = "DEGRADED"  # PARTIAL_SUCCESS
    FAILED = "FAILED"  # FAILED
    UNKNOWN = "UNKNOWN"  # no run recorded


_RUN_HEALTH = {
    "SUCCESS": RunHealth.HEALTHY,
    "PARTIAL_SUCCESS": RunHealth.DEGRADED,
    "FAILED": RunHealth.FAILED,
}


def run_health(run: Optional[RunRecord]) -> RunHealth:
    if run is None:
        return RunHealth.UNKNOWN
    return _RUN_HEALTH.get(run.status, RunHealth.UNKNOWN)


def collection_summary(run: Optional[RunRecord]) -> str:
    """One line on the latest attempt: never phrased as a refresh unless it succeeded."""
    if run is None:
        return "No collection run recorded"
    attempted = len(run.banks_attempted) or len(run.banks_successful) + len(run.banks_failed)
    failed = len(run.banks_failed)
    health = run_health(run)
    if health is RunHealth.HEALTHY:
        return f"Latest collection succeeded: {len(run.banks_successful)} of {attempted} banks"
    if health is RunHealth.DEGRADED:
        return f"Latest collection partially succeeded: {failed} of {attempted} banks failed"
    if health is RunHealth.FAILED:
        return f"Latest collection failed: {failed} of {attempted} banks failed"
    return f"Latest collection status {run.status!r} is not recognised"


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
    admitted_keys: Optional[Collection[str]] = None,
) -> list[CompetitorRateRecord]:
    """Records eligible for the market snapshot, before the staleness check.

    The same filters ``build_market_snapshot`` applies (comparable tier or an
    ``admitted_keys`` term-deposit special, tracked bank and term, no ``non_comparable`` or
    name-excluded product). Exclusion and health logic must use this, not a looser or
    narrower record set, or they would describe records the market view never considered.
    """
    markers = tuple(name.lower() for name in excluded_product_names)
    admitted = admitted_keys or ()
    return [
        r
        for r in records
        if r.rate_category != "non_comparable"
        and (
            rate_category is None
            or r.rate_category == rate_category
            or r.observation_key() in admitted
        )
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


def _hours(age: Optional[float]) -> str:
    return f"{age:.0f} hours old" if age is not None else "of unknown age"


@dataclass(frozen=True)
class OperationalFreshness:
    """One bank's operational state: observation freshness *and* the latest attempt.

    The two facts stay separate fields; ``state`` combines them. Observation timestamps are
    never rewritten: a failed attempt leaves ``observation.oldest_confirmation`` exactly where
    it was, and a retried or empty ledger row never makes an old reading look new.
    """

    bank: str
    state: OperationalState
    observation: ObservationFreshness
    collection: CollectionOutcome
    latest_attempt_at: Optional[datetime]
    latest_attempt_status: Optional[str]
    latest_attempt_run_id: Optional[str]
    #: The bank's last successful collection (collector health), when known.
    last_success_at: Optional[datetime]
    reason: str

    @property
    def label(self) -> str:
        if self.state is OperationalState.QUARANTINED:
            return "Quarantined readings"
        if self.state is OperationalState.FRESH:
            return "Fresh"
        title = {
            OperationalState.DEGRADED: "Degraded",
            OperationalState.STALE: "Stale",
            OperationalState.UNKNOWN: "Unknown",
        }[self.state]
        return f"{title} — {self.reason}"


def assess_operational_freshness(
    observation: ObservationFreshness,
    latest_run: Optional[RunRecord],
    *,
    last_success_at: Optional[datetime] = None,
) -> OperationalFreshness:
    """Combine one bank's observation freshness with the product's latest operational run.

    The single rule set behind the freshness frame, system health and the dashboard. Evaluated
    in order: quarantine (Phase 2a), then stale observations (excluded from the market view
    whatever the latest attempt did), then no trusted observation, then no operational ledger
    (UNKNOWN -- old observation rows alone never prove a successful collection), then the
    bank's own outcome in the latest attempt.
    """
    age = observation.age_hours
    if latest_run is None:
        collection = CollectionOutcome.UNKNOWN
    elif observation.bank in latest_run.banks_failed:
        collection = CollectionOutcome.FAILED
    elif observation.bank in latest_run.banks_successful:
        collection = CollectionOutcome.SUCCEEDED
    else:
        collection = CollectionOutcome.NOT_ATTEMPTED
    failed_note = "; last collection failed" if collection is CollectionOutcome.FAILED else ""

    if observation.quarantined:
        state = OperationalState.QUARANTINED
        reason = "a reading awaits operator review; the last confirmed rate is used" + failed_note
    elif observation.state is FreshnessState.STALE:
        state = OperationalState.STALE
        reason = (
            f"latest trusted observations are {_hours(age)}; excluded from market statistics"
            + failed_note
        )
    elif observation.state is FreshnessState.UNKNOWN:
        state = OperationalState.UNKNOWN
        reason = "no trusted observation recorded" + failed_note
    elif collection is CollectionOutcome.UNKNOWN:
        state = OperationalState.UNKNOWN
        reason = (
            "no operational collection run recorded; observations are "
            f"{_hours(age)} (legacy evidence, not a confirmed collection)"
        )
    elif collection is CollectionOutcome.FAILED:
        state = OperationalState.DEGRADED
        reason = f"last collection failed; latest trusted observations are {_hours(age)}"
    elif collection is CollectionOutcome.NOT_ATTEMPTED:
        state = OperationalState.DEGRADED
        reason = (
            f"not attempted in the latest collection; latest trusted observations are "
            f"{_hours(age)}"
        )
    elif observation.not_fully_confirmed:
        state = OperationalState.DEGRADED
        reason = (
            "latest collection did not reconfirm every current observation; oldest is "
            f"{_hours(age)}"
        )
    else:
        state = OperationalState.FRESH
        reason = f"collected in the latest run; oldest trusted observation is {_hours(age)}"

    return OperationalFreshness(
        bank=observation.bank,
        state=state,
        observation=observation,
        collection=collection,
        latest_attempt_at=latest_run.started_at if latest_run else None,
        latest_attempt_status=latest_run.status if latest_run else None,
        latest_attempt_run_id=latest_run.run_id if latest_run else None,
        last_success_at=last_success_at,
        reason=reason,
    )


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
    #: Observation freshness combined with the latest operational attempt (DATA-004).
    operational: Optional[OperationalFreshness] = None


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
    #: Pending operator-review quarantines and any escalated past the policy threshold.
    quarantine: Optional[QuarantineStatus] = None

    @property
    def fresh_bank_count(self) -> int:
        """Banks whose trusted observations are inside the freshness window (rate age only)."""
        return sum(1 for b in self.banks if b.freshness.state is FreshnessState.FRESH)

    @property
    def confirmed_fresh_bank_count(self) -> int:
        """Banks that are fresh *and* were collected successfully in the latest attempt."""
        return sum(
            1
            for b in self.banks
            if b.operational is not None and b.operational.state is OperationalState.FRESH
        )

    @property
    def run_health(self) -> RunHealth:
        return run_health(self.latest_run)

    @property
    def collection_summary(self) -> str:
        return collection_summary(self.latest_run)

    @property
    def oldest_observation_age_hours(self) -> Optional[float]:
        """Age of the oldest trusted observation still inside the freshness window."""
        ages = [
            b.freshness.age_hours
            for b in self.banks
            if b.freshness.state is FreshnessState.FRESH and b.freshness.age_hours is not None
        ]
        return max(ages, default=None)


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
    if (
        freshness.state is FreshnessState.UNKNOWN
        and collector is CollectorStatus.UNKNOWN
        and not freshness.quarantined
    ):
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
    resolved = service.get_resolved_current_observations(product_type=product)
    comparable = comparable_records(
        resolved,
        rate_category=snapshot.rate_category,
        banks=set(config.selected_banks),
        terms=set(config.tracked_terms_months),
        excluded_product_names=config.excluded_product_names,
        admitted_keys=service.admitted_special_keys(resolved, product, snapshot.rate_category),
    )
    quarantine = service.get_quarantine_status(product_type=product, now=now)
    # Raw warning rows remain visible even when a category flip resolves to another tier. A
    # reading an operator has already rejected or voided is untrusted but no longer an open
    # quarantine, so it stops counting the bank as quarantined.
    decided = store.get_decided_untrusted_keys(product)
    quarantined_banks = {
        r.bank
        for r in records
        if r.validation_status == "warning"
        and r.observation_key() not in decided
        and r.term_months_normalised in config.tracked_terms_months
    }
    run_ids_by_bank = store.get_current_observation_run_ids(product_type=product)
    latest_run_id = store.latest_run_id(product_type=product)
    collectors = {c.bank: c for c in store.get_collector_health(product_type=product)}
    # The latest *attempt* for this product, not the latest observation write: a total failure
    # never reaches record_observations(), so only operational_runs can show it (DATA-004).
    latest_attempt = store.get_latest_run_record(product_type=product)

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
        if bank in quarantined_banks:
            freshness = replace(freshness, quarantined=True, not_fully_confirmed=False)
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
                operational=assess_operational_freshness(
                    freshness,
                    latest_attempt,
                    last_success_at=entry.last_success_at if entry else None,
                ),
            )
        )
    return SystemHealth(
        evaluated_at=now,
        product_type=product,
        database_ok=report.ok,
        database_issues=tuple(report.issues),
        latest_run=latest_attempt,
        banks=tuple(banks),
        snapshot_valid=bool(snapshot.terms),
        telegram_configured=build_telegram_sink_from_env() is not None,
        webhook_configured=build_webhook_sink_from_env() is not None,
        max_observation_age_hours=config.max_observation_age_hours,
        quarantine=quarantine,
    )
