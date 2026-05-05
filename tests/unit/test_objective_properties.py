"""Property tests (T3, T20): objective decomposition reconciles; zero-elasticity closed form."""
from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from balance_pricing_engine.diagnostics import (
    ObjectiveDecomposition,
    objective_decomposition_from_result,
)
from balance_pricing_engine.optimization.grid_search import OptimizationResult

_finite = st.floats(allow_nan=False, allow_infinity=False, min_value=-1e9, max_value=1e9)
_nonneg = st.floats(allow_nan=False, allow_infinity=False, min_value=0.0, max_value=1e9)


@given(
    income=_finite,
    ecl=_nonneg,
    deposit_cost=_nonneg,
    wholesale_cost=_nonneg,
    penalty=_nonneg,
    loan_volume=_nonneg,
    deposit_volume=_nonneg,
    shortfall=_nonneg,
)
@settings(max_examples=500)
def test_decomposition_reconciled_total_equals_arithmetic_formula(
    income, ecl, deposit_cost, wholesale_cost, penalty,
    loan_volume, deposit_volume, shortfall,
):
    """T3: reconciled_total_objective always equals the explicit income–cost formula."""
    expected = income - ecl - deposit_cost - wholesale_cost - penalty
    decomp = ObjectiveDecomposition(
        loan_interest_income=income,
        expected_credit_loss=ecl,
        deposit_interest_cost=deposit_cost,
        wholesale_funding_cost=wholesale_cost,
        funding_shortfall_penalty=penalty,
        total_objective=expected,
        expected_loan_volume=loan_volume,
        expected_deposit_volume=deposit_volume,
        funding_shortfall=shortfall,
    )
    assert math.isclose(decomp.reconciled_total_objective, expected, rel_tol=1e-9, abs_tol=1e-9)


@given(
    income=_finite,
    ecl=_nonneg,
    deposit_cost=_nonneg,
    wholesale_cost=_nonneg,
    penalty=_nonneg,
    loan_volume=_nonneg,
    deposit_volume=_nonneg,
    shortfall=_nonneg,
)
@settings(max_examples=500)
def test_objective_decomposition_from_result_reconciles(
    income, ecl, deposit_cost, wholesale_cost, penalty,
    loan_volume, deposit_volume, shortfall,
):
    """T3: objective_decomposition_from_result preserves component consistency."""
    total = income - ecl - deposit_cost - wholesale_cost - penalty
    result = OptimizationResult(
        loan_rate=0.07,
        deposit_rate=0.04,
        loan_interest_income=income,
        expected_credit_loss=ecl,
        deposit_interest_cost=deposit_cost,
        wholesale_funding_cost=wholesale_cost,
        funding_shortfall_penalty=penalty,
        total_objective=total,
        expected_loan_volume=loan_volume,
        expected_deposit_volume=deposit_volume,
        funding_shortfall=shortfall,
    )
    decomp = objective_decomposition_from_result(result)
    assert math.isclose(decomp.reconciled_total_objective, decomp.total_objective, rel_tol=1e-9, abs_tol=1e-9)
