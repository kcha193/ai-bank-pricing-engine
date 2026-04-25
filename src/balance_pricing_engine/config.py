"""Configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class GridConfig(BaseModel):
    start: float
    stop: float
    step: float


class SimulationConfig(BaseModel):
    n_loan_customers: int = Field(gt=0)
    n_deposit_customers: int = Field(gt=0)


class MarketConfig(BaseModel):
    cash_rate: float
    wholesale_funding_spread: float
    competitor_loan_rate: float
    competitor_deposit_rate: float


class ProductConfig(BaseModel):
    loan_rate_grid: GridConfig
    deposit_rate_grid: GridConfig


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
    products: ProductConfig
    balance_sheet: BalanceSheetConfig
    response: ResponseConfig


def load_config(path: str | Path) -> EngineConfig:
    """Load an engine configuration from YAML."""
    with Path(path).open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle)
    return EngineConfig.model_validate(raw_config)

