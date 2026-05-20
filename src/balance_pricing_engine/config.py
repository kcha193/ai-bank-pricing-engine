"""Configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Optional
import warnings

import yaml
from pydantic import BaseModel, Field, model_validator

from balance_pricing_engine.competitor_rates.models import CompetitorRatesConfig
from balance_pricing_engine.market import bps_to_rate


class GridConfig(BaseModel):
    start: float
    stop: float
    step: float

    @model_validator(mode="after")
    def validate_grid(self) -> "GridConfig":
        if self.step <= 0:
            raise ValueError("Grid step must be greater than zero.")
        if self.stop < self.start:
            raise ValueError("Grid stop must be greater than or equal to start.")
        return self


class SpreadGridConfigModel(BaseModel):
    min: float
    max: float
    step: float

    @model_validator(mode="after")
    def validate_spread_grid(self) -> "SpreadGridConfigModel":
        if self.step <= 0:
            raise ValueError("Spread grid step must be greater than zero.")
        if self.max < self.min:
            raise ValueError("Spread grid max must be greater than or equal to min.")
        return self


class SimulationConfig(BaseModel):
    n_loan_customers: int = Field(gt=0)
    n_deposit_customers: int = Field(gt=0)


class MarketConfig(BaseModel):
    cash_rate: float
    wholesale_funding_spread: float
    competitor_loan_rate: float
    competitor_deposit_rate: float
    home_loan_reference_rate: Optional[float] = Field(default=None, ge=0)
    term_deposit_reference_rate: Optional[float] = Field(default=None, ge=0)


class ProductConfig(BaseModel):
    loan_rate_grid: GridConfig
    deposit_rate_grid: GridConfig


class LoanTierConfig(BaseModel):
    name: str
    lvr_max: Optional[float] = None
    rate_grid: GridConfig


class DepositTierConfig(BaseModel):
    name: str
    balance_max: Optional[float] = None
    rate_grid: GridConfig


class SegmentedPricingConfig(BaseModel):
    loan_tiers: list[LoanTierConfig] = Field(min_length=1)
    deposit_tiers: list[DepositTierConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tiers(self) -> "SegmentedPricingConfig":
        _validate_tier_sequence(
            tier_name="loan_tiers",
            tiers=[(tier.name, tier.lvr_max) for tier in self.loan_tiers],
        )
        _validate_tier_sequence(
            tier_name="deposit_tiers",
            tiers=[(tier.name, tier.balance_max) for tier in self.deposit_tiers],
        )
        return self


class PricingStrategyConfig(BaseModel):
    home_loan_spread_bps: SpreadGridConfigModel
    term_deposit_spread_bps: SpreadGridConfigModel


class BalanceSheetConfig(BaseModel):
    required_deposit_funding_ratio: float = Field(ge=0, le=1)
    funding_shortfall_penalty_rate: float = Field(ge=0)
    loan_rwa_weight: float = Field(default=0.35, ge=0, le=2.5)
    capital_hurdle_rate: float = Field(default=0.12, ge=0, le=1)


class MultiPeriodConfig(BaseModel):
    n_periods: int = Field(default=2, ge=2)
    loan_runoff_rate: float = Field(default=0.30, ge=0, le=1)
    deposit_runoff_rate: float = Field(default=0.50, ge=0, le=1)
    discount_rate: float = Field(default=0.08, ge=0)
    market_cash_rate_drift_bps: float = 0.0


class LoanResponseCoefficientsConfig(BaseModel):
    intercept: float = 1.10
    rate_anchor: float = 0.065
    risk_score_coef: float = -1.20
    credit_score_coef: float = 0.35
    lvr_coef: float = -0.25


class DepositResponseCoefficientsConfig(BaseModel):
    intercept: float = -0.65
    rate_anchor: float = 0.045
    rate_sensitivity_coef: float = 0.60
    relationship_coef: float = 0.04
    digital_coef: float = -0.20


class ResponseCoefficientsConfig(BaseModel):
    loan: LoanResponseCoefficientsConfig = Field(default_factory=LoanResponseCoefficientsConfig)
    deposit: DepositResponseCoefficientsConfig = Field(
        default_factory=DepositResponseCoefficientsConfig
    )


class ResponseConfig(BaseModel):
    loan_rate_elasticity: float
    loan_competitor_gap_elasticity: float
    deposit_rate_elasticity: float
    deposit_competitor_gap_elasticity: float
    coefficients: ResponseCoefficientsConfig = Field(default_factory=ResponseCoefficientsConfig)


class EngineConfig(BaseModel):
    random_seed: int
    simulation: SimulationConfig
    market: MarketConfig
    products: Optional[ProductConfig] = None
    pricing_strategy: Optional[PricingStrategyConfig] = None
    segmented_pricing: Optional[SegmentedPricingConfig] = None
    competitor_rates: Optional[CompetitorRatesConfig] = None
    balance_sheet: BalanceSheetConfig
    response: ResponseConfig
    multiperiod: Optional[MultiPeriodConfig] = None

    @model_validator(mode="after")
    def validate_pricing_configuration(self) -> "EngineConfig":
        pricing_modes = [
            self.pricing_strategy is not None,
            self.products is not None,
            self.segmented_pricing is not None,
        ]
        if sum(pricing_modes) != 1:
            raise ValueError(
                "Exactly one of pricing_strategy, products, or segmented_pricing must be provided."
            )
        if self.pricing_strategy is not None:
            if self.market.home_loan_reference_rate is None:
                raise ValueError("market.home_loan_reference_rate is required for pricing_strategy.")
            if self.market.term_deposit_reference_rate is None:
                raise ValueError("market.term_deposit_reference_rate is required for pricing_strategy.")
            min_home_loan_rate = (
                self.market.home_loan_reference_rate
                + bps_to_rate(self.pricing_strategy.home_loan_spread_bps.min)
            )
            min_term_deposit_rate = (
                self.market.term_deposit_reference_rate
                + bps_to_rate(self.pricing_strategy.term_deposit_spread_bps.min)
            )
            if min_home_loan_rate < 0:
                raise ValueError("pricing_strategy.home_loan_spread_bps generates negative rates.")
            if min_term_deposit_rate < 0:
                raise ValueError("pricing_strategy.term_deposit_spread_bps generates negative rates.")
            self._warn_if_spread_band_is_narrow()
        self._warn_for_business_sanity()
        return self

    def _warn_for_business_sanity(self) -> None:
        if self.market.wholesale_funding_spread < 0:
            warnings.warn(
                "negative wholesale_funding_spread may imply below-cash wholesale funding.",
                stacklevel=2,
            )
        if (
            self.balance_sheet.funding_shortfall_penalty_rate
            < self.market.wholesale_funding_spread
        ):
            warnings.warn(
                "funding_shortfall_penalty_rate is below wholesale_funding_spread; "
                "penalty dominated by wholesale funding economics.",
                stacklevel=2,
            )
        if (
            self.simulation.n_loan_customers < 1000
            or self.simulation.n_deposit_customers < 1000
        ):
            warnings.warn(
                "n_loan_customers or n_deposit_customers below 1000 may create high MC variance.",
                stacklevel=2,
            )

    def _warn_if_spread_band_is_narrow(self) -> None:
        if self.pricing_strategy is None:
            return
        spread_bands = {
            "home_loan_spread_bps": self.pricing_strategy.home_loan_spread_bps,
            "term_deposit_spread_bps": self.pricing_strategy.term_deposit_spread_bps,
        }
        for name, spread_band in spread_bands.items():
            if spread_band.max - spread_band.min < 50:
                warnings.warn(
                    f"configured {name} band width is less than 50 bps; "
                    "likely to expand past policy.",
                    stacklevel=2,
                )


def _validate_tier_sequence(tier_name: str, tiers: list[tuple[str, Optional[float]]]) -> None:
    names = [name for name, _ in tiers]
    if len(names) != len(set(names)):
        raise ValueError(f"{tier_name} names must be unique.")

    none_count = sum(bound is None for _, bound in tiers)
    if none_count > 1:
        raise ValueError(f"{tier_name} may contain at most one catch-all tier.")

    if any(bound is None for _, bound in tiers[:-1]):
        raise ValueError(f"{tier_name} must be sorted ascending with catch-all tier last.")

    numeric_bounds = [bound for _, bound in tiers if bound is not None]
    if any(bound <= 0 for bound in numeric_bounds):
        raise ValueError(f"{tier_name} upper bounds must be greater than zero.")
    if any(a >= b for a, b in zip(numeric_bounds, numeric_bounds[1:])):
        raise ValueError(
            f"{tier_name} must have strictly increasing upper bounds (duplicates not allowed)."
        )


def load_config(path: str | Path) -> EngineConfig:
    """Load an engine configuration from YAML."""
    with Path(path).open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle)
    return EngineConfig.model_validate(raw_config)
