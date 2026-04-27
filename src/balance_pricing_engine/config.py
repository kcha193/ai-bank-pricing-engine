"""Configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

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


class PricingStrategyConfig(BaseModel):
    home_loan_spread_bps: SpreadGridConfigModel
    term_deposit_spread_bps: SpreadGridConfigModel


class BalanceSheetConfig(BaseModel):
    required_deposit_funding_ratio: float = Field(ge=0, le=1)
    funding_shortfall_penalty_rate: float = Field(ge=0)


class ResponseConfig(BaseModel):
    loan_rate_elasticity: float
    loan_competitor_gap_elasticity: float
    deposit_rate_elasticity: float
    deposit_competitor_gap_elasticity: float


class EngineConfig(BaseModel):
    random_seed: int
    simulation: SimulationConfig
    market: MarketConfig
    products: Optional[ProductConfig] = None
    pricing_strategy: Optional[PricingStrategyConfig] = None
    competitor_rates: Optional[CompetitorRatesConfig] = None
    balance_sheet: BalanceSheetConfig
    response: ResponseConfig

    @model_validator(mode="after")
    def validate_pricing_configuration(self) -> "EngineConfig":
        if self.pricing_strategy is None and self.products is None:
            raise ValueError("Either pricing_strategy or products rate grids must be provided.")
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
        return self


def load_config(path: str | Path) -> EngineConfig:
    """Load an engine configuration from YAML."""
    with Path(path).open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle)
    return EngineConfig.model_validate(raw_config)
