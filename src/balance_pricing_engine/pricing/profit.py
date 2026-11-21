"""Risk-adjusted profit calculations."""

from __future__ import annotations

from typing import Union

import numpy as np
import pandas as pd

from balance_pricing_engine.pricing.credit_loss import (
    ANNUAL_EXPECTED_LOSS_COLUMN,
    _numeric_array,
    _require_range,
)

# Portfolio-level terms accept a scalar or a NumPy array of candidate portfolios. Scalar
# inputs keep returning plain Python floats; arrays are evaluated element by element with
# the same arithmetic, so a vectorised search ranks candidates on identical values.
FloatOrArray = Union[float, np.ndarray]

TIER1_CAPITAL_RATIO = 0.10


def non_negative_part(value: FloatOrArray) -> FloatOrArray:
    """Return ``max(value, 0)`` for a scalar, or elementwise for an array."""
    if np.ndim(value) == 0:
        return max(value, 0.0)
    return np.maximum(value, 0.0)


def expected_loan_income_after_losses(
    customers: pd.DataFrame,
    acceptance_probability: np.ndarray,
    loan_rate: float,
) -> float:
    """Calculate expected annualized loan interest income after credit losses."""
    return expected_loan_interest_income(
        customers,
        acceptance_probability,
        loan_rate,
    ) - expected_credit_loss(customers, acceptance_probability)


def expected_loan_interest_income(
    customers: pd.DataFrame,
    acceptance_probability: np.ndarray,
    loan_rate: float,
) -> float:
    """Calculate expected annualized gross loan interest income."""
    balances = customers["loan_amount"].to_numpy()
    return float(np.sum(acceptance_probability * balances * loan_rate))


def expected_credit_loss(customers: pd.DataFrame, acceptance_probability: np.ndarray) -> float:
    """Calculate expected annualized credit loss for accepted home loans."""
    if ANNUAL_EXPECTED_LOSS_COLUMN in customers:
        annual_loss = _numeric_array("Annual expected loss", customers[ANNUAL_EXPECTED_LOSS_COLUMN])
        probabilities = _numeric_array("Acceptance probabilities", acceptance_probability)
        if annual_loss.ndim != 1 or probabilities.shape != annual_loss.shape:
            raise ValueError("Acceptance probabilities must align with facility losses.")
        _require_range("Acceptance probabilities", probabilities, 1)
        _require_range("Annual expected loss", annual_loss)
        with np.errstate(over="ignore"):
            total = float(np.sum(probabilities * annual_loss))
        if not np.isfinite(total):
            raise ValueError("Portfolio expected credit loss must be finite.")
        return total

    # Preserve the original order of multiplication for legacy economic artifacts.
    balances = customers["loan_amount"].to_numpy()
    expected_loss = customers["expected_loss_rate"].to_numpy()
    return float(np.sum(acceptance_probability * balances * expected_loss))


def expected_capital_charge(
    loan_volume: FloatOrArray,
    rwa_weight: float,
    capital_hurdle_rate: float,
    tier1_capital_ratio: float = TIER1_CAPITAL_RATIO,
) -> FloatOrArray:
    """Calculate annual cost of capital tied up in loan-book RWA."""
    rwa = loan_volume * rwa_weight
    required_capital = rwa * tier1_capital_ratio
    return required_capital * capital_hurdle_rate


def maturity_adjusted_rwa_weight(base_rwa_weight: float, tenor_years: float | None) -> float:
    """Return a maturity-scaled RWA weight using a simplified linear approximation.

    When tenor is not configured, the base RWA weight is returned unchanged.
    Clamps tenor to [1, 5] years. Factor = 1.00 + 0.06 * (tenor - 1).
    At 1y: 1.00x base. At 5y: 1.24x base. Not a regulatory calculation.
    """
    if tenor_years is None:
        return base_rwa_weight
    clamped = max(1.0, min(5.0, tenor_years))
    return base_rwa_weight * (1.0 + 0.06 * (clamped - 1.0))


def expected_deposit_cost(
    customers: pd.DataFrame,
    uptake_probability: np.ndarray,
    deposit_rate: float,
) -> float:
    """Calculate expected annualized interest expense for term deposits."""
    balances = customers["deposit_amount"].to_numpy()
    return float(np.sum(uptake_probability * balances * deposit_rate))


def expected_loan_volume(customers: pd.DataFrame, acceptance_probability: np.ndarray) -> float:
    """Calculate expected home loan balances booked."""
    return float(np.sum(acceptance_probability * customers["loan_amount"].to_numpy()))


def expected_deposit_volume(customers: pd.DataFrame, uptake_probability: np.ndarray) -> float:
    """Calculate expected term deposit balances raised."""
    return float(np.sum(uptake_probability * customers["deposit_amount"].to_numpy()))


def expected_wholesale_funding_cost(
    loan_volume: FloatOrArray,
    deposit_volume: FloatOrArray,
    wholesale_funding_rate: float,
) -> FloatOrArray:
    """Calculate funding cost for loans not funded by term deposits."""
    wholesale_funded_volume = non_negative_part(loan_volume - deposit_volume)
    return wholesale_funded_volume * wholesale_funding_rate


def expected_excess_deposit_income(
    loan_volume: FloatOrArray,
    deposit_volume: FloatOrArray,
    cash_rate: float,
) -> FloatOrArray:
    """Calculate cash-rate income earned on deposits raised above loan volume."""
    excess_deposit_volume = non_negative_part(deposit_volume - loan_volume)
    return excess_deposit_volume * cash_rate


def portfolio_return_on_equity(
    loan_interest_income: FloatOrArray,
    credit_loss: FloatOrArray,
    deposit_interest_cost: FloatOrArray,
    wholesale_funding_cost: FloatOrArray,
    funding_shortfall_penalty: FloatOrArray,
    excess_deposit_income: FloatOrArray,
    loan_volume: FloatOrArray,
    rwa_weight: float,
    tier1_capital_ratio: float = TIER1_CAPITAL_RATIO,
    *,
    acquisition_cost: FloatOrArray = 0.0,
    concentration_penalty: FloatOrArray = 0.0,
) -> FloatOrArray:
    """Return profit before the capital charge over required tier-1 capital.

    Zero required capital (no loan volume) gives 0.0, matching the joint grid search.
    """
    required_capital = loan_volume * rwa_weight * tier1_capital_ratio
    profit_before_capital = (
        loan_interest_income
        - credit_loss
        - deposit_interest_cost
        - wholesale_funding_cost
        - funding_shortfall_penalty
        + excess_deposit_income
        - acquisition_cost
        - concentration_penalty
    )
    if np.ndim(required_capital) == 0 and np.ndim(profit_before_capital) == 0:
        return profit_before_capital / required_capital if required_capital > 0 else 0.0
    required_capital, profit_before_capital = np.broadcast_arrays(
        required_capital, profit_before_capital
    )
    roe = np.zeros(required_capital.shape, dtype=float)
    positive = required_capital > 0
    roe[positive] = profit_before_capital[positive] / required_capital[positive]
    return roe
