import numpy as np

from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.profit import (
    expected_loan_profit,
    expected_wholesale_funding_cost,
)
from balance_pricing_engine.synthetic.customers import generate_loan_customers
from balance_pricing_engine.utils.random import make_rng


def test_expected_credit_loss_reduces_loan_profit():
    customers = generate_loan_customers(100, make_rng(10))
    probabilities = np.ones(len(customers))

    base_profit = expected_loan_profit(customers, probabilities, 0.07, 0.055)

    higher_loss_customers = customers.copy()
    higher_loss_customers["expected_loss_rate"] = customers["expected_loss_rate"] + 0.01
    higher_loss_profit = expected_loan_profit(higher_loss_customers, probabilities, 0.07, 0.055)

    assert higher_loss_profit < base_profit


def test_funding_shortfall_creates_penalty():
    result = calculate_funding_result(
        loan_volume=1_000_000,
        deposit_volume=500_000,
        required_deposit_funding_ratio=0.75,
        shortfall_penalty_rate=0.02,
    )

    assert result.required_deposits == 750_000
    assert result.funding_shortfall == 250_000
    assert result.funding_penalty == 5_000


def test_wholesale_cost_and_shortfall_penalty_are_distinct_additive_charges():
    loan_volume = 1_000_000
    deposit_volume = 400_000

    wholesale_cost = expected_wholesale_funding_cost(
        loan_volume=loan_volume,
        deposit_volume=deposit_volume,
        wholesale_funding_rate=0.05,
    )
    funding = calculate_funding_result(
        loan_volume=loan_volume,
        deposit_volume=deposit_volume,
        required_deposit_funding_ratio=0.75,
        shortfall_penalty_rate=0.02,
    )

    assert wholesale_cost == 30_000
    assert funding.funding_shortfall == 350_000
    assert funding.funding_penalty == 7_000
    assert wholesale_cost + funding.funding_penalty == 37_000
