"""Transparent joint grid-search optimizer."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.profit import (
    expected_deposit_cost,
    expected_deposit_volume,
    expected_loan_income_after_losses,
    expected_loan_volume,
    expected_wholesale_funding_cost,
)
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)


@dataclass(frozen=True)
class OptimizationResult:
    loan_rate: float
    deposit_rate: float
    objective_value: float
    loan_income_after_losses: float
    deposit_cost: float
    wholesale_funding_cost: float
    funding_penalty: float
    expected_loan_volume: float
    expected_deposit_volume: float
    funding_shortfall: float


def build_rate_grid(start: float, stop: float, step: float) -> np.ndarray:
    """Build an inclusive decimal rate grid with stable rounding."""
    n_steps = int(round((stop - start) / step))
    return np.round(start + np.arange(n_steps + 1) * step, 6)


def optimize_joint_rates(
    loan_customers: pd.DataFrame,
    deposit_customers: pd.DataFrame,
    market: MarketState,
    loan_rate_grid: np.ndarray,
    deposit_rate_grid: np.ndarray,
    loan_rate_elasticity: float,
    loan_competitor_gap_elasticity: float,
    deposit_rate_elasticity: float,
    deposit_competitor_gap_elasticity: float,
    required_deposit_funding_ratio: float,
    funding_shortfall_penalty_rate: float,
) -> OptimizationResult:
    """Optimize portfolio-level loan and deposit rates by exhaustive grid search."""
    best_result: OptimizationResult | None = None

    for loan_rate in loan_rate_grid:
        loan_probs = loan_acceptance_probability(
            loan_customers,
            float(loan_rate),
            market.competitor_loan_rate,
            loan_rate_elasticity,
            loan_competitor_gap_elasticity,
        )
        loan_volume = expected_loan_volume(loan_customers, loan_probs)
        loan_income_after_losses = expected_loan_income_after_losses(
            loan_customers,
            loan_probs,
            float(loan_rate),
        )

        for deposit_rate in deposit_rate_grid:
            deposit_probs = deposit_uptake_probability(
                deposit_customers,
                float(deposit_rate),
                market.competitor_deposit_rate,
                deposit_rate_elasticity,
                deposit_competitor_gap_elasticity,
            )
            deposit_volume = expected_deposit_volume(deposit_customers, deposit_probs)
            deposit_cost = expected_deposit_cost(deposit_customers, deposit_probs, float(deposit_rate))
            wholesale_funding_cost = expected_wholesale_funding_cost(
                loan_volume,
                deposit_volume,
                market.wholesale_funding_rate,
            )
            funding = calculate_funding_result(
                loan_volume,
                deposit_volume,
                required_deposit_funding_ratio,
                funding_shortfall_penalty_rate,
            )
            objective_value = (
                loan_income_after_losses
                - deposit_cost
                - wholesale_funding_cost
                - funding.funding_penalty
            )

            result = OptimizationResult(
                loan_rate=float(loan_rate),
                deposit_rate=float(deposit_rate),
                objective_value=objective_value,
                loan_income_after_losses=loan_income_after_losses,
                deposit_cost=deposit_cost,
                wholesale_funding_cost=wholesale_funding_cost,
                funding_penalty=funding.funding_penalty,
                expected_loan_volume=loan_volume,
                expected_deposit_volume=deposit_volume,
                funding_shortfall=funding.funding_shortfall,
            )
            if best_result is None or result.objective_value > best_result.objective_value:
                best_result = result

    if best_result is None:
        raise ValueError("At least one loan rate and one deposit rate are required.")
    return best_result
