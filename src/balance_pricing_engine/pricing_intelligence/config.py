"""Central configuration for the NZ pricing-intelligence layer.

Every threshold, timeout, and path used by collection, validation, change
detection, and market-state classification is declared here so no magic numbers
are scattered through the pipeline. Values are overridable from YAML via
``EngineConfig.pricing_intelligence``.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator

DEFAULT_USER_AGENT = (
    "ai-bank-pricing-engine/0.1 (+pricing-intelligence; public advertised rates only)"
)

# Fixed home-loan terms tracked in phase 1, in months.
DEFAULT_TRACKED_TERMS_MONTHS: tuple[int, ...] = (6, 12, 18, 24, 36, 48, 60)


class CollectionConfig(BaseModel):
    """HTTP behaviour for public bank page collection."""

    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(default=2.0, ge=0)
    # Politeness delay between requests to the same host. Public pages only; this
    # keeps collection well below anything a bank would consider aggressive.
    inter_request_delay_seconds: float = Field(default=1.0, ge=0)
    user_agent: str = DEFAULT_USER_AGENT


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


class PricingIntelligenceConfig(BaseModel):
    """Top-level pricing-intelligence settings."""

    database_path: str = "data/processed/pricing_intelligence/observations.db"
    tracked_terms_months: list[int] = Field(
        default_factory=lambda: list(DEFAULT_TRACKED_TERMS_MONTHS)
    )
    selected_banks: list[str] = Field(
        default_factory=lambda: ["ANZ", "ASB", "Westpac", "BNZ", "Kiwibank"]
    )
    # The bank whose position is reported in competitive-position features. None
    # means "we are not one of the tracked public banks" (the default for the
    # synthetic engine), and positioning is computed against a supplied rate.
    our_bank: Optional[str] = None
    collection: CollectionConfig = Field(default_factory=CollectionConfig)
    validation: ValidationThresholds = Field(default_factory=ValidationThresholds)
    market_state: MarketStateThresholds = Field(default_factory=MarketStateThresholds)

    @model_validator(mode="after")
    def validate_terms(self) -> "PricingIntelligenceConfig":
        if not self.tracked_terms_months:
            raise ValueError("tracked_terms_months must not be empty")
        if any(term <= 0 for term in self.tracked_terms_months):
            raise ValueError("tracked_terms_months must be positive")
        if len(set(self.tracked_terms_months)) != len(self.tracked_terms_months):
            raise ValueError("tracked_terms_months must not contain duplicates")
        return self
