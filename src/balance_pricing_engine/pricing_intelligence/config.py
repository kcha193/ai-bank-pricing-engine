"""Central configuration for the NZ pricing-intelligence layer.

Every threshold, timeout, and path used by collection, validation, change
detection, and market-state classification is declared here so no magic numbers
are scattered through the pipeline. Values are overridable from YAML via
``EngineConfig.pricing_intelligence``.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator

from balance_pricing_engine.competitor_rates.collection_config import CollectionConfig
from balance_pricing_engine.pricing_intelligence.coverage import (
    DEFAULT_AGED_INPUT_HOURS,
    DEFAULT_MIN_BANKS,
)
from balance_pricing_engine.secrets.provider import EnvSecretsProvider, SecretsProvider

# Fixed home-loan terms tracked in phase 1, in months.
DEFAULT_TRACKED_TERMS_MONTHS: tuple[int, ...] = (6, 12, 18, 24, 36, 48, 60)

#: Env var documented in .env.example to relocate the operational database
#: outside the repo checkout. Matches the ANTHROPIC_API_KEY / PRICING_ENGINE_API_KEYS
#: / PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL pattern (see :mod:`.secrets.provider`).
DATABASE_PATH_SECRET_NAME = "PRICING_INTELLIGENCE_DB_PATH"


#: Default SQLite busy timeout (DATA-009). A writer waits this long for another
#: connection's write lock before failing with "database is locked". Long enough to ride
#: out the other product's commit, a backup or a brief manual session; short enough that a
#: genuinely stuck holder surfaces as a classified failure well inside the run deadline.
DEFAULT_BUSY_TIMEOUT_SECONDS = 30.0
#: Upper bound on the configurable busy timeout: waits stay bounded.
MAX_BUSY_TIMEOUT_SECONDS = 300.0


class PeerPlausibilityThresholds(BaseModel):
    """Peer-market sanity signal for a moving rate (DATA-016).

    A movement under ``suspicious_change_bps`` can still be a misread (a +60 bps
    special/standard mix-up). This compares the candidate with *comparable peers*
    -- other banks with the same product, term, rate category, balance tier and
    eligibility, observed recently enough -- and raises a **review** signal when it
    both sits far from the peer median **and** moved far more than the peers did.
    It never rejects or approves a rate on its own.

    "Leader side" is the direction that wins the market ranking when wrong: above
    peers for a term deposit (highest rate leads), below peers for a home loan
    (lowest rate leads). Those are the dangerous misreads, so they get the tighter
    bound; the opposite side uses ``other_side_deviation_bps``.
    """

    enabled: bool = True
    # Fewer comparable peers than this is "insufficient evidence": no median is
    # fabricated and the check abstains.
    min_peers: int = Field(default=3, ge=2)
    # Peers older than this relative to the candidate's observation are ignored.
    max_peer_age_hours: float = Field(default=168.0, gt=0)
    # Balance tiers are compared by *band of the advertised minimum*, not exact equality: banks
    # quote different retail minimums (none, $1,000, $5,000) for what is the same everyday
    # tier, and demanding an exact match leaves a real market with no comparable peer at all.
    # A minimum below the first edge is the retail tier; each further edge starts a larger-
    # balance tier ($500k+ specials are not comparable to retail). Maximums are not banded.
    balance_tier_edges: tuple[float, ...] = (10_000.0, 100_000.0)
    leader_side_deviation_bps: float = Field(default=40.0, gt=0)
    other_side_deviation_bps: float = Field(default=75.0, gt=0)
    # The candidate must also have moved this much more than the peer median move.
    movement_excess_bps: float = Field(default=40.0, gt=0)
    # Dispersion guard: in a market whose peers already disagree widely, a deviation
    # must also exceed this many median-absolute-deviations (floored) to count.
    dispersion_multiple: float = Field(default=4.0, ge=0)
    dispersion_floor_bps: float = Field(default=5.0, ge=0)


    @model_validator(mode="after")
    def validate_tier_edges(self) -> "PeerPlausibilityThresholds":
        edges = list(self.balance_tier_edges)
        if any(edge <= 0 for edge in edges) or edges != sorted(set(edges)):
            raise ValueError("balance_tier_edges must be positive and strictly ascending")
        return self


class ValidationThresholds(BaseModel):
    """Plausibility bounds applied to every parsed advertised rate."""

    min_mortgage_rate_pct: float = Field(default=0.5, ge=0)
    max_mortgage_rate_pct: float = Field(default=15.0, gt=0)
    min_deposit_rate_pct: float = Field(default=0.0, ge=0)
    max_deposit_rate_pct: float = Field(default=15.0, gt=0)
    # A single-observation move larger than this is treated as suspicious: it is
    # far more often a parser regression or a special/standard mix-up than a
    # genuine repricing.
    suspicious_change_bps: float = Field(default=75.0, gt=0)
    # A bank reporting SUCCESS after extracting fewer than this fraction of its
    # previously known product range is more likely a page-layout regression
    # than a mass withdrawal: below this ratio (records extracted this run
    # divided by keys previously known for the bank), it is treated as not
    # reliably collected for withdrawal detection -- the same treatment as an
    # outright fetch failure.
    min_bank_coverage_ratio: float = Field(default=0.5, gt=0, le=1.0)
    peer: PeerPlausibilityThresholds = Field(default_factory=PeerPlausibilityThresholds)

    @model_validator(mode="after")
    def validate_bounds(self) -> "ValidationThresholds":
        if self.max_mortgage_rate_pct <= self.min_mortgage_rate_pct:
            raise ValueError("max_mortgage_rate_pct must exceed min_mortgage_rate_pct")
        if self.max_deposit_rate_pct <= self.min_deposit_rate_pct:
            raise ValueError("max_deposit_rate_pct must exceed min_deposit_rate_pct")
        return self


class MarketStateThresholds(BaseModel):
    """Cut-offs for the deterministic market-state classifier.

    Each band is the *minimum* activity required to reach that state. Bands are
    evaluated from the most severe downwards; see
    :mod:`balance_pricing_engine.pricing_intelligence.market_state`.
    """

    lookback_days: int = Field(default=7, gt=0)

    high_changes: int = Field(default=8, ge=0)
    high_banks_changed: int = Field(default=4, ge=0)

    elevated_changes: int = Field(default=4, ge=0)
    elevated_banks_changed: int = Field(default=2, ge=0)

    normal_changes: int = Field(default=1, ge=0)

    # A single large average movement signals a repricing round even when few
    # individual changes have landed yet.
    large_mean_move_bps: float = Field(default=15.0, gt=0)
    # Share of moves in one direction required to call a dominant direction.
    directional_dominance_ratio: float = Field(default=0.7, gt=0.5, le=1.0)

    @model_validator(mode="after")
    def validate_monotonic_bands(self) -> "MarketStateThresholds":
        if self.high_changes < self.elevated_changes:
            raise ValueError("high_changes must be >= elevated_changes")
        if self.elevated_changes < self.normal_changes:
            raise ValueError("elevated_changes must be >= normal_changes")
        if self.high_banks_changed < self.elevated_banks_changed:
            raise ValueError("high_banks_changed must be >= elevated_banks_changed")
        return self


class HealthThresholds(BaseModel):
    """Escalation policy for a collector's consecutive failures.

    A single failed collection is routine (a bank site hiccups); the alerting
    burden should scale with how many *in a row* have failed. Bands are
    inclusive minimums, evaluated by
    :func:`~.orchestration.classify_health_escalation`: below
    ``warning_after_failures`` is logged only, at least ``warning_after_failures``
    but below ``alert_after_failures`` is a structured warning log (no Telegram
    send), and at least ``alert_after_failures`` sends a Telegram health alert.
    """

    warning_after_failures: int = Field(default=2, ge=1)
    alert_after_failures: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def validate_monotonic(self) -> "HealthThresholds":
        if self.alert_after_failures < self.warning_after_failures:
            raise ValueError("alert_after_failures must be >= warning_after_failures")
        return self


class QuarantinePolicy(BaseModel):
    """Operational escalation for readings left in quarantine.

    A quarantined reading is invisible to the market view by design, so one that
    nobody resolves must not stay quiet. ``escalation_after_runs`` counts distinct
    collection runs (a retry of one run_id never counts twice) in which the same
    reading identity was observed and still unreviewed. At or above it the run is
    degraded (exit code 2), health reports an escalation and one alert is raised.
    """

    escalation_after_runs: int = Field(default=3, ge=1)


class DepositSpecialPolicy(BaseModel):
    """Evidence phrases for term-deposit special comparability (DATA-001).

    See :mod:`.deposit_specials`. Matched case-insensitively as whole phrases against a
    special's own row text and column heading. A restriction phrase makes a special
    not comparable; an availability phrase is the only positive evidence that admits it to
    the ordinary term-deposit benchmark. These are evidence vocabularies, not assumptions:
    a special matching neither stays *uncertain* and degrades the run.
    """

    restriction_markers: tuple[str, ...] = (
        "new money", "new funds", "new deposits only", "new customers only",
        "existing customers only", "for new customers", "for existing customers",
        "online only", "apply online", "online applications only", "selected customers",
        "private bank", "premium banking", "members only", "business customers",
        "conditions apply", "eligibility criteria", "by invitation", "rollover", "reinvest",
    )  # fmt: skip
    availability_markers: tuple[str, ...] = (
        "available to all", "all customers", "new and existing customers",
        "existing and new customers", "open to all", "open to everyone",
    )  # fmt: skip

    @model_validator(mode="after")
    def validate_markers(self) -> "DepositSpecialPolicy":
        markers = self.restriction_markers + self.availability_markers
        if any(not marker.strip() for marker in markers):
            raise ValueError("deposit special markers must not be blank")
        return self


class SkipEscalationPolicy(BaseModel):
    """Escalation for consecutive scheduled runs skipped because another run held the
    product's lock (exit code 3; DATA-008).

    One skip is routine (a slow run overlapping the next slot). A streak means a stuck
    collector, a hung browser or a stale process. Mirrors :class:`HealthThresholds`: at
    ``warning_after_consecutive_skips`` a structured warning is logged; at
    ``alert_after_consecutive_skips`` one operational alert is delivered per streak to each
    configured channel. Counts are per product and survive process restarts (see
    ``pricing_intelligence.skip_escalation``).
    """

    warning_after_consecutive_skips: int = Field(default=2, ge=1)
    alert_after_consecutive_skips: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def validate_monotonic(self) -> "SkipEscalationPolicy":
        if self.alert_after_consecutive_skips < self.warning_after_consecutive_skips:
            raise ValueError(
                "alert_after_consecutive_skips must be >= warning_after_consecutive_skips"
            )
        return self


class PricingIntelligenceConfig(BaseModel):
    """Top-level pricing-intelligence settings."""

    database_path: str = "data/processed/pricing_intelligence/observations.db"
    tracked_terms_months: list[int] = Field(
        default_factory=lambda: list(DEFAULT_TRACKED_TERMS_MONTHS)
    )
    selected_banks: list[str] = Field(
        default_factory=lambda: ["ANZ", "ASB", "Westpac", "BNZ", "Kiwibank"]
    )
    # Excluded from pricing benchmarks and competitive-position features. None
    # means "we are not one of the tracked public banks" (the default for the
    # synthetic engine), and positioning is computed against a supplied rate.
    our_bank: Optional[str] = None
    # A bank's last-known rate stops feeding market-derived pricing once it is
    # older than this. An unreachable bank is "unknown, not withdrawn" (so it
    # stays in change-detection's baseline indefinitely), but an unknown value
    # left in a live pricing view forever is itself a risk: MARKET_LEADER or
    # MARKET_MEDIAN could otherwise keep tracking a months-stale rate.
    max_observation_age_hours: float = Field(default=168.0, gt=0)
    # Minimum *distinct eligible* banks behind a market-derived competitor assumption
    # (MARKET_MEDIAN/MEAN/LEADER/SECOND_LOWEST), per product and term, after every trust and
    # comparability rule and the own-bank exclusion (DATA-003). Below it the assumption is
    # refused, never silently replaced by the configured rate. See pricing_intelligence.coverage.
    min_banks: int = Field(default=DEFAULT_MIN_BANKS, ge=1, strict=True)
    # An input still inside the freshness window but older than this is reported as "aged" on
    # the benchmark evidence (not excluded): old inputs stay visible before they turn stale.
    aged_input_hours: float = Field(default=DEFAULT_AGED_INPUT_HOURS, gt=0)
    # Product names that are structurally not comparable to standard fixed
    # lending -- e.g. ASB's construction-only "Back My Build" (see
    # ASBCollector) -- and so must not win MARKET_LEADER/MARKET_MEDIAN purely
    # because their niche rate happens to be the lowest. Matched
    # case-insensitively as a substring of the record's product_name.
    excluded_product_names: tuple[str, ...] = ("Back My Build",)
    collection: CollectionConfig = Field(default_factory=CollectionConfig)
    validation: ValidationThresholds = Field(default_factory=ValidationThresholds)
    market_state: MarketStateThresholds = Field(default_factory=MarketStateThresholds)
    health: HealthThresholds = Field(default_factory=HealthThresholds)
    quarantine: QuarantinePolicy = Field(default_factory=QuarantinePolicy)
    deposit_specials: DepositSpecialPolicy = Field(default_factory=DepositSpecialPolicy)
    # SQLite busy timeout for the writable observation store (DATA-009); see
    # ObservationStore. Bounded so a stuck writer surfaces as a classified failure.
    sqlite_busy_timeout_seconds: float = Field(
        default=DEFAULT_BUSY_TIMEOUT_SECONDS, gt=0, le=MAX_BUSY_TIMEOUT_SECONDS
    )
    skip_escalation: SkipEscalationPolicy = Field(default_factory=SkipEscalationPolicy)

    @model_validator(mode="after")
    def validate_terms(self) -> "PricingIntelligenceConfig":
        if not self.tracked_terms_months:
            raise ValueError("tracked_terms_months must not be empty")
        if any(term <= 0 for term in self.tracked_terms_months):
            raise ValueError("tracked_terms_months must be positive")
        if len(set(self.tracked_terms_months)) != len(self.tracked_terms_months):
            raise ValueError("tracked_terms_months must not contain duplicates")
        # Only an *explicit* minimum is checked against the tracked set: the default is a
        # policy, and a deliberately small tracked set must still load (its market strategies
        # then fail at resolution with a coverage message rather than at config load).
        if "min_banks" in self.model_fields_set and self.min_banks > len(self.selected_banks):
            raise ValueError(
                f"min_banks ({self.min_banks}) exceeds the {len(self.selected_banks)} selected "
                "banks; no market-derived benchmark could ever be resolved"
            )
        return self


def resolve_database_path_from_env(
    config: PricingIntelligenceConfig, secrets: Optional[SecretsProvider] = None
) -> PricingIntelligenceConfig:
    """Apply the documented ``PRICING_INTELLIGENCE_DB_PATH`` override, if set.

    Neither the CLI nor the scheduled script otherwise reads this env var --
    both instantiate ``PricingIntelligenceConfig()`` and use its hard-coded
    default path unless ``--database``/``--config`` is passed. Callers should
    apply this to that freshly constructed default, *before* any explicit
    ``--config``/``--database`` override, so an operator's explicit choice on
    the command line always wins over the environment.
    """
    secrets = EnvSecretsProvider() if secrets is None else secrets
    database_path = secrets.get_secret(DATABASE_PATH_SECRET_NAME)
    if not database_path:
        return config
    return config.model_copy(update={"database_path": database_path})
