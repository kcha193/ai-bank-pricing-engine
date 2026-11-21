"""TEST-002: metamorphic partial-effect invariants of the synthetic response model.

Each test holds the customer population and every other input fixed, perturbs one input,
and checks the documented direction of the response. Only effects that the model *must*
have are asserted. Loan interest income and portfolio profit are deliberately not asserted
to be monotone in the loan rate: a higher rate raises the yield per accepted loan but can
lower volume (see ``test_loan_income_is_not_required_to_be_monotone``).

Existing coverage not repeated here: probabilities in [0, 1] and zero-elasticity rate
independence (``test_response_properties.py``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from economic_strategies import (
    budget,
    deposit_coefficients,
    deposit_customers,
    deposit_elasticities,
    increasing_rate_pair,
    loan_coefficients,
    loan_customers,
    loan_elasticities,
    rates,
    reference_deposit_probability,
    reference_loan_probability,
)
from hypothesis import given
from hypothesis import strategies as st

from balance_pricing_engine.optimization.grid_search import evaluate_joint_rate_grid
from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.profit import (
    expected_credit_loss,
    expected_deposit_cost,
    expected_deposit_volume,
    expected_loan_volume,
)
from balance_pricing_engine.synthetic.market import MarketState
from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)
from balance_pricing_engine.config import ResponseCoefficientsConfig

pytestmark = pytest.mark.economic_invariants

# A strictly interior probability changes visibly in float64 when utility moves by >= 1e-4.
_INTERIOR = (1e-6, 1.0 - 1e-6)


def _loan_p(customers, offered, competitor, own, gap, coef):
    return loan_acceptance_probability(customers, offered, competitor, own, gap, coefficients=coef)


def _deposit_p(customers, offered, competitor, own, gap, coef):
    return deposit_uptake_probability(customers, offered, competitor, own, gap, coefficients=coef)


# ------------------------------------------------------------------ loan response


@given(
    customers=loan_customers(),
    pair=increasing_rate_pair(),
    competitor=rates,
    own=loan_elasticities,
    gap=loan_elasticities,
    coef=loan_coefficients(),
)
@budget(150)
def test_r_l01_higher_loan_rate_never_raises_acceptance(customers, pair, competitor, own, gap, coef):
    """Loan A/G/H: r_low < r_high => p(r_high) <= p(r_low), volume and ECL non-increasing.

    When own + gap < 0 the decrease is strict wherever p is not numerically saturated.
    """
    low, high = pair
    p_low = _loan_p(customers, low, competitor, own, gap, coef)
    p_high = _loan_p(customers, high, competitor, own, gap, coef)
    assert np.all(p_high <= p_low)

    slope = own + gap
    if slope * (high - low) <= -1e-4:
        interior = (p_low > _INTERIOR[0]) & (p_low < _INTERIOR[1])
        assert np.all(p_high[interior] < p_low[interior])

    # G: expected booked volume, holding the population fixed.
    assert expected_loan_volume(customers, p_high) <= expected_loan_volume(customers, p_low)
    # H: ECL with non-negative balances and loss rates cannot rise when acceptance falls.
    assert expected_credit_loss(customers, p_high) <= expected_credit_loss(customers, p_low)


@given(
    customers=loan_customers(),
    offered=rates,
    competitor_pair=increasing_rate_pair(),
    own=loan_elasticities,
    gap=loan_elasticities,
    coef=loan_coefficients(),
)
@budget(150)
def test_r_l02_higher_competitor_loan_rate_never_lowers_acceptance(
    customers, offered, competitor_pair, own, gap, coef
):
    """Loan B: gap = ours - competitor with gap elasticity <= 0, so a dearer competitor helps."""
    competitor_low, competitor_high = competitor_pair
    p_vs_cheap = _loan_p(customers, offered, competitor_low, own, gap, coef)
    p_vs_dear = _loan_p(customers, offered, competitor_high, own, gap, coef)
    assert np.all(p_vs_dear >= p_vs_cheap)


@given(
    customers=loan_customers(),
    offered=rates,
    competitor_a=rates,
    competitor_b=rates,
    own=loan_elasticities,
    coef=loan_coefficients(),
)
@budget(100)
def test_r_l03_zero_gap_elasticity_makes_competitor_loan_rate_irrelevant(
    customers, offered, competitor_a, competitor_b, own, coef
):
    """Loan D: with gap elasticity 0 the competitor rate drops out of the utility exactly."""
    np.testing.assert_array_equal(
        _loan_p(customers, offered, competitor_a, own, 0.0, coef),
        _loan_p(customers, offered, competitor_b, own, 0.0, coef),
    )


@given(
    customers=loan_customers(),
    offered=rates,
    competitor=rates,
    own=loan_elasticities,
    gap=loan_elasticities,
    coef=loan_coefficients(),
)
@budget(150)
def test_r_l04_loan_probability_matches_independent_reference_and_is_finite(
    customers, offered, competitor, own, gap, coef
):
    """Loan E/F + reference: finite, in [0, 1], equal to the documented utility by hand."""
    actual = _loan_p(customers, offered, competitor, own, gap, coef)
    expected = reference_loan_probability(customers, offered, competitor, own, gap, coef)
    assert np.all(np.isfinite(actual))
    assert np.all((actual >= 0.0) & (actual <= 1.0))
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-15)


# --------------------------------------------------------------- deposit response


@given(
    customers=deposit_customers(),
    pair=increasing_rate_pair(),
    competitor=rates,
    own=deposit_elasticities,
    gap=deposit_elasticities,
    coef=deposit_coefficients(),
)
@budget(150)
def test_r_d01_higher_deposit_rate_never_lowers_uptake_volume_or_expense(
    customers, pair, competitor, own, gap, coef
):
    """Deposit A/D/E: uptake, expected volume and interest expense are non-decreasing."""
    low, high = pair
    p_low = _deposit_p(customers, low, competitor, own, gap, coef)
    p_high = _deposit_p(customers, high, competitor, own, gap, coef)
    assert np.all(p_high >= p_low)

    if (own + gap) * (high - low) >= 1e-4:
        interior = (p_low > _INTERIOR[0]) & (p_low < _INTERIOR[1])
        assert np.all(p_high[interior] > p_low[interior])

    assert expected_deposit_volume(customers, p_high) >= expected_deposit_volume(customers, p_low)
    # E: non-negative rate x non-decreasing uptake x non-negative balance.
    assert expected_deposit_cost(customers, p_high, high) >= expected_deposit_cost(
        customers, p_low, low
    )


@given(
    customers=deposit_customers(),
    offered=rates,
    competitor_pair=increasing_rate_pair(),
    own=deposit_elasticities,
    gap=deposit_elasticities,
    coef=deposit_coefficients(),
)
@budget(150)
def test_r_d02_higher_competing_deposit_rate_never_raises_uptake(
    customers, offered, competitor_pair, own, gap, coef
):
    """Deposit B: gap = ours - competitor with gap elasticity >= 0."""
    competitor_low, competitor_high = competitor_pair
    p_vs_low = _deposit_p(customers, offered, competitor_low, own, gap, coef)
    p_vs_high = _deposit_p(customers, offered, competitor_high, own, gap, coef)
    assert np.all(p_vs_high <= p_vs_low)


@given(
    customers=deposit_customers(),
    offered=rates,
    competitor_a=rates,
    competitor_b=rates,
    own=deposit_elasticities,
    coef=deposit_coefficients(),
)
@budget(100)
def test_r_d03_zero_gap_elasticity_makes_competing_deposit_rate_irrelevant(
    customers, offered, competitor_a, competitor_b, own, coef
):
    np.testing.assert_array_equal(
        _deposit_p(customers, offered, competitor_a, own, 0.0, coef),
        _deposit_p(customers, offered, competitor_b, own, 0.0, coef),
    )


@given(
    customers=deposit_customers(),
    offered=rates,
    competitor=rates,
    own=deposit_elasticities,
    gap=deposit_elasticities,
    coef=deposit_coefficients(),
)
@budget(150)
def test_r_d04_deposit_probability_matches_independent_reference_and_is_finite(
    customers, offered, competitor, own, gap, coef
):
    """Deposit H + reference."""
    actual = _deposit_p(customers, offered, competitor, own, gap, coef)
    expected = reference_deposit_probability(customers, offered, competitor, own, gap, coef)
    assert np.all(np.isfinite(actual))
    assert np.all((actual >= 0.0) & (actual <= 1.0))
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-15)


@given(
    customers=deposit_customers(),
    pair=increasing_rate_pair(),
    competitor=rates,
    own=deposit_elasticities,
    gap=deposit_elasticities,
    ratio=st.floats(0.1, 1.0),
    coverage_position=st.floats(0.0, 2.0),
    penalty=st.floats(0.0, 0.3),
)
@budget(100)
def test_r_d05_more_deposits_never_increase_shortfall_at_fixed_loans(
    customers, pair, competitor, own, gap, ratio, coverage_position, penalty
):
    """A higher TD offer cannot worsen modelled funding at fixed loan volume."""
    low, high = pair
    deposit_low = expected_deposit_volume(
        customers, deposit_uptake_probability(customers, low, competitor, own, gap)
    )
    deposit_high = expected_deposit_volume(
        customers, deposit_uptake_probability(customers, high, competitor, own, gap)
    )
    # Put the requirement inside or beyond the change in volume to exercise both
    # a shortfall that disappears and one that shrinks but remains.
    required = deposit_low + (deposit_high - deposit_low) * coverage_position
    loan_volume = required / ratio
    before = calculate_funding_result(loan_volume, deposit_low, ratio, penalty)
    after = calculate_funding_result(loan_volume, deposit_high, ratio, penalty)
    assert after.funding_shortfall <= before.funding_shortfall
    assert after.funding_penalty <= before.funding_penalty


# ------------------------------------------------- cross-product separability (I, F)


@st.composite
def _joint_problem(draw):
    loans = draw(loan_customers(max_size=12))
    deposits = draw(deposit_customers(max_size=12))
    market = MarketState(
        cash_rate=draw(st.floats(0.0, 0.08)),
        wholesale_funding_spread=draw(st.floats(0.0, 0.03)),
        competitor_loan_rate=draw(rates),
        competitor_deposit_rate=draw(rates),
    )
    loan_grid = np.array(sorted(set(draw(st.lists(rates, min_size=2, max_size=4)))))
    deposit_grid = np.array(sorted(set(draw(st.lists(rates, min_size=2, max_size=4)))))
    elasticities = {
        "loan_rate_elasticity": draw(loan_elasticities),
        "loan_competitor_gap_elasticity": draw(loan_elasticities),
        "deposit_rate_elasticity": draw(deposit_elasticities),
        "deposit_competitor_gap_elasticity": draw(deposit_elasticities),
    }
    return loans, deposits, market, loan_grid, deposit_grid, elasticities


_LOAN_SIDE = ["loan_interest_income", "expected_credit_loss", "capital_charge", "expected_loan_volume"]
_DEPOSIT_SIDE = ["deposit_interest_cost", "expected_deposit_volume"]


@given(problem=_joint_problem(), ratio=st.floats(0, 1), penalty=st.floats(0, 0.3))
@budget(60)
def test_r_x01_loan_and_deposit_responses_are_separable_on_the_joint_grid(
    problem, ratio, penalty
):
    """Loan I + deposit F: a loan-rate change leaves deposit-side terms untouched, and vice
    versa. Only the funding terms couple the two products."""
    loans, deposits, market, loan_grid, deposit_grid, elasticities = problem
    grid = evaluate_joint_rate_grid(
        loans,
        deposits,
        market,
        loan_grid,
        deposit_grid,
        required_deposit_funding_ratio=ratio,
        funding_shortfall_penalty_rate=penalty,
        response_coefficients=ResponseCoefficientsConfig(),
        **elasticities,
    )
    for _, by_deposit in grid.groupby("deposit_rate"):
        for column in _DEPOSIT_SIDE:
            assert by_deposit[column].nunique() == 1, column
    for _, by_loan in grid.groupby("loan_rate"):
        for column in _LOAN_SIDE:
            assert by_loan[column].nunique() == 1, column


def test_loan_income_is_not_required_to_be_monotone():
    """Guard against asserting a false property: with a steep own-rate elasticity, gross loan
    interest income falls as the rate rises, because volume falls faster than yield rises."""
    customers = pd.DataFrame(
        {
            "risk_score": [0.2],
            "credit_score": [760.0],
            "lvr": [0.7],
            "loan_amount": [500_000.0],
            "expected_loss_rate": [0.002],
        }
    )
    coef = ResponseCoefficientsConfig().loan
    income = []
    for rate in (0.05, 0.07):
        p = _loan_p(customers, rate, 0.05, -400.0, -100.0, coef)
        income.append(float(np.sum(p * customers["loan_amount"].to_numpy() * rate)))
    assert income[1] < income[0]
