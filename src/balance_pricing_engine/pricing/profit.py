"""Risk-adjusted profit calculations."""

from __future__ import annotations

import numpy as np
import pandas as pd


def expected_loan_profit(
    customers: pd.DataFrame,
    acceptance_probability: np.ndarray,
    loan_rate: float,
    funding_rate: float,
) -> float:
    """Calculate expected annualized risk-adjusted home loan profit."""
    balances = customers["loan_amount"].to_numpy()
    expected_loss = customers["expected_loss_rate"].to_numpy()
    margin = loan_rate - funding_rate - expected_loss
    return float(np.sum(acceptance_probability * balances * margin))


def expected_loan_income_after_losses(
    customers: pd.DataFrame,
    acceptance_probability: np.ndarray,
    loan_rate: float,
) -> float:
    """Calculate expected annualized loan interest income after credit losses."""
    balances = customers["loan_amount"].to_numpy()
    expected_loss = customers["expected_loss_rate"].to_numpy()
    return float(np.sum(acceptance_probability * balances * (loan_rate - expected_loss)))


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
    loan_volume: float,
    deposit_volume: float,
    wholesale_funding_rate: float,
) -> float:
    """Calculate funding cost for loans not funded by term deposits."""
    wholesale_funded_volume = max(loan_volume - deposit_volume, 0.0)
    return wholesale_funded_volume * wholesale_funding_rate
