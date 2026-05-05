"""Property tests (T1): response probabilities are always in [0, 1]."""
from __future__ import annotations

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)

_rate = st.floats(0.0, 0.30, allow_nan=False, allow_infinity=False)
_elasticity = st.floats(-200.0, 200.0, allow_nan=False, allow_infinity=False)


@st.composite
def _loan_customers(draw):
    n = draw(st.integers(1, 30))
    return pd.DataFrame(
        {
            "risk_score": np.array(
                draw(st.lists(st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False), min_size=n, max_size=n))
            ),
            "credit_score": np.array(
                draw(st.lists(st.floats(500.0, 850.0, allow_nan=False, allow_infinity=False), min_size=n, max_size=n))
            ),
            "lvr": np.array(
                draw(st.lists(st.floats(0.45, 0.98, allow_nan=False, allow_infinity=False), min_size=n, max_size=n))
            ),
        }
    )


@st.composite
def _deposit_customers(draw):
    n = draw(st.integers(1, 30))
    return pd.DataFrame(
        {
            "rate_sensitivity": np.array(
                draw(st.lists(st.floats(0.05, 0.95, allow_nan=False, allow_infinity=False), min_size=n, max_size=n))
            ),
            "relationship_years": np.array(
                draw(st.lists(st.floats(0.0, 35.0, allow_nan=False, allow_infinity=False), min_size=n, max_size=n))
            ),
            "digital_engagement": np.array(
                draw(st.lists(st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False), min_size=n, max_size=n))
            ),
        }
    )


@given(
    customers=_loan_customers(),
    offered_rate=_rate,
    competitor_rate=_rate,
    rate_elasticity=_elasticity,
    competitor_gap_elasticity=_elasticity,
)
@settings(max_examples=200)
def test_loan_acceptance_probability_is_in_unit_interval(
    customers, offered_rate, competitor_rate, rate_elasticity, competitor_gap_elasticity
):
    probs = loan_acceptance_probability(
        customers, offered_rate, competitor_rate, rate_elasticity, competitor_gap_elasticity
    )
    assert probs.shape == (len(customers),)
    assert np.all(probs >= 0.0)
    assert np.all(probs <= 1.0)


@given(
    customers=_deposit_customers(),
    offered_rate=_rate,
    competitor_rate=_rate,
    rate_elasticity=_elasticity,
    competitor_gap_elasticity=_elasticity,
)
@settings(max_examples=200)
def test_deposit_uptake_probability_is_in_unit_interval(
    customers, offered_rate, competitor_rate, rate_elasticity, competitor_gap_elasticity
):
    probs = deposit_uptake_probability(
        customers, offered_rate, competitor_rate, rate_elasticity, competitor_gap_elasticity
    )
    assert probs.shape == (len(customers),)
    assert np.all(probs >= 0.0)
    assert np.all(probs <= 1.0)


@given(
    customers=_loan_customers(),
    rate1=_rate,
    rate2=_rate,
    competitor_rate=_rate,
)
@settings(max_examples=200)
def test_zero_loan_elasticity_makes_acceptance_rate_independent(
    customers, rate1, rate2, competitor_rate
):
    """T20: with zero elasticities acceptance probability does not depend on offered rate."""
    prob1 = loan_acceptance_probability(customers, rate1, competitor_rate, 0.0, 0.0)
    prob2 = loan_acceptance_probability(customers, rate2, competitor_rate, 0.0, 0.0)
    np.testing.assert_array_equal(prob1, prob2)


@given(
    customers=_deposit_customers(),
    rate1=_rate,
    rate2=_rate,
    competitor_rate=_rate,
)
@settings(max_examples=200)
def test_zero_deposit_elasticity_makes_uptake_rate_independent(
    customers, rate1, rate2, competitor_rate
):
    """T20: with zero elasticities deposit uptake does not depend on offered rate."""
    prob1 = deposit_uptake_probability(customers, rate1, competitor_rate, 0.0, 0.0)
    prob2 = deposit_uptake_probability(customers, rate2, competitor_rate, 0.0, 0.0)
    np.testing.assert_array_equal(prob1, prob2)
