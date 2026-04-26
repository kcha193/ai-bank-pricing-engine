"""Market-relative pricing grid helpers."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class MarketRates:
    """Reference market rates used to anchor product pricing."""

    home_loan_reference_rate: float
    term_deposit_reference_rate: float


@dataclass(frozen=True)
class SpreadGridConfig:
    """Inclusive spread grid definition in basis points."""

    min: float
    max: float
    step: float


def bps_to_rate(basis_points: float) -> float:
    """Convert basis points to a decimal rate."""
    return basis_points / 10_000


def build_market_relative_grid(
    market_rates: MarketRates,
    home_loan_spread_bps: SpreadGridConfig,
    term_deposit_spread_bps: SpreadGridConfig,
) -> tuple[np.ndarray, np.ndarray]:
    """Build home loan and term deposit rate grids from market-relative spreads."""
    if market_rates.home_loan_reference_rate < 0:
        raise ValueError("Home loan reference rate must be non-negative.")
    if market_rates.term_deposit_reference_rate < 0:
        raise ValueError("Term deposit reference rate must be non-negative.")

    home_loan_spreads = _build_spread_rate_grid(home_loan_spread_bps)
    term_deposit_spreads = _build_spread_rate_grid(term_deposit_spread_bps)
    home_loan_grid = market_rates.home_loan_reference_rate + home_loan_spreads
    term_deposit_grid = market_rates.term_deposit_reference_rate + term_deposit_spreads
    if np.any(home_loan_grid < 0):
        raise ValueError("Home loan spread grid generates negative rates.")
    if np.any(term_deposit_grid < 0):
        raise ValueError("Term deposit spread grid generates negative rates.")
    return np.round(home_loan_grid, 6), np.round(term_deposit_grid, 6)


def _build_spread_rate_grid(config: SpreadGridConfig) -> np.ndarray:
    if config.step <= 0:
        raise ValueError("Spread grid step must be greater than zero.")
    if config.max < config.min:
        raise ValueError("Spread grid max must be greater than or equal to min.")
    n_steps = int(round((config.max - config.min) / config.step))
    spread_bps_grid = config.min + np.arange(n_steps + 1) * config.step
    return np.array([bps_to_rate(float(spread_bps)) for spread_bps in spread_bps_grid])
