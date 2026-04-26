"""Configuration-driven pricing grid builders."""

from __future__ import annotations

import numpy as np

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.market import (
    MarketRates,
    SpreadGridConfig,
    build_market_relative_grid,
)
from balance_pricing_engine.optimization.grid_search import build_rate_grid


def build_configured_rate_grids(config: EngineConfig) -> tuple[np.ndarray, np.ndarray]:
    """Build pricing grids from market-relative spreads, falling back to absolute grids."""
    if config.pricing_strategy is not None:
        market_rates = MarketRates(
            home_loan_reference_rate=float(config.market.home_loan_reference_rate),
            term_deposit_reference_rate=float(config.market.term_deposit_reference_rate),
        )
        return build_market_relative_grid(
            market_rates=market_rates,
            home_loan_spread_bps=SpreadGridConfig(
                **config.pricing_strategy.home_loan_spread_bps.model_dump()
            ),
            term_deposit_spread_bps=SpreadGridConfig(
                **config.pricing_strategy.term_deposit_spread_bps.model_dump()
            ),
        )

    if config.products is None:
        raise ValueError("Either pricing_strategy or products rate grids must be provided.")
    return (
        build_rate_grid(**config.products.loan_rate_grid.model_dump()),
        build_rate_grid(**config.products.deposit_rate_grid.model_dump()),
    )
