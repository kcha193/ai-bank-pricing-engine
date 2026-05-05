"""Risk-adjusted profit calculations."""

from __future__ import annotations

import numpy as np
import pandas as pd


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
    balances = customers["loan_amount"].to_numpy()
    expected_loss = customers["expected_loss_rate"].to_numpy()
    return float(np.sum(acceptance_probability * balances * expected_loss))


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


def expected_excess_deposit_income(
    loan_volume: float,
    deposit_volume: float,
    cash_rate: float,
) -> float:
    """Calculate cash-rate income earned on deposits raised above loan volume."""
    excess_deposit_volume = max(deposit_volume - loan_volume, 0.0)
    return excess_deposit_volume * cash_rate
