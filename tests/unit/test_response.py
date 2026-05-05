from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.synthetic.response import (
    deposit_uptake_probability,
    loan_acceptance_probability,
)
from balance_pricing_engine.utils.random import make_rng
from balance_pricing_engine.config import load_config


def test_home_loan_acceptance_decreases_when_rate_increases():
    customers = generate_loan_customers(1000, make_rng(1))

    low_rate = loan_acceptance_probability(customers, 0.062, 0.067, -85.0, -65.0).mean()
    high_rate = loan_acceptance_probability(customers, 0.075, 0.067, -85.0, -65.0).mean()

    assert high_rate < low_rate


def test_term_deposit_uptake_increases_when_rate_increases():
    customers = generate_deposit_customers(1000, make_rng(2))

    low_rate = deposit_uptake_probability(customers, 0.040, 0.045, 95.0, 75.0).mean()
    high_rate = deposit_uptake_probability(customers, 0.052, 0.045, 95.0, 75.0).mean()

    assert high_rate > low_rate


def test_competitor_rates_influence_customer_response():
    loan_customers = generate_loan_customers(1000, make_rng(3))
    deposit_customers = generate_deposit_customers(1000, make_rng(4))

    loan_response_when_competitor_is_cheap = loan_acceptance_probability(
        loan_customers, 0.067, 0.061, -85.0, -65.0
    ).mean()
    loan_response_when_competitor_is_expensive = loan_acceptance_probability(
        loan_customers, 0.067, 0.073, -85.0, -65.0
    ).mean()
    deposit_response_when_competitor_is_cheap = deposit_uptake_probability(
        deposit_customers, 0.045, 0.052, 95.0, 75.0
    ).mean()
    deposit_response_when_competitor_is_expensive = deposit_uptake_probability(
        deposit_customers, 0.045, 0.039, 95.0, 75.0
    ).mean()

    assert loan_response_when_competitor_is_cheap < loan_response_when_competitor_is_expensive
    assert deposit_response_when_competitor_is_cheap < deposit_response_when_competitor_is_expensive


def test_configured_response_coefficients_match_legacy_defaults():
    config = load_config("configs/base.yaml")
    loan_customers = generate_loan_customers(1000, make_rng(13))
    deposit_customers = generate_deposit_customers(1000, make_rng(14))

    legacy_loan = loan_acceptance_probability(
        loan_customers,
        0.066,
        0.0675,
        config.response.loan_rate_elasticity,
        config.response.loan_competitor_gap_elasticity,
    )
    configured_loan = loan_acceptance_probability(
        loan_customers,
        0.066,
        0.0675,
        config.response.loan_rate_elasticity,
        config.response.loan_competitor_gap_elasticity,
        coefficients=config.response.coefficients.loan,
    )
    legacy_deposit = deposit_uptake_probability(
        deposit_customers,
        0.046,
        0.045,
        config.response.deposit_rate_elasticity,
        config.response.deposit_competitor_gap_elasticity,
    )
    configured_deposit = deposit_uptake_probability(
        deposit_customers,
        0.046,
        0.045,
        config.response.deposit_rate_elasticity,
        config.response.deposit_competitor_gap_elasticity,
        coefficients=config.response.coefficients.deposit,
    )

    assert configured_loan.tolist() == legacy_loan.tolist()
    assert configured_deposit.tolist() == legacy_deposit.tolist()
