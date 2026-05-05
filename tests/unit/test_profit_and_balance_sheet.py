import numpy as np

from balance_pricing_engine.diagnostics import ObjectiveDecomposition
from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.profit import (
    expected_credit_loss,
    expected_loan_interest_income,
    expected_wholesale_funding_cost,
)
from balance_pricing_engine.synthetic.customers import generate_loan_customers
from balance_pricing_engine.utils.random import make_rng


def test_expected_credit_loss_reduces_loan_profit():
    customers = generate_loan_customers(100, make_rng(10))
    probabilities = np.ones(len(customers))

    base_decomposition = ObjectiveDecomposition(
        loan_interest_income=expected_loan_interest_income(customers, probabilities, 0.07),
        expected_credit_loss=expected_credit_loss(customers, probabilities),
        deposit_interest_cost=0.0,
        wholesale_funding_cost=0.0,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=0.0,
        total_objective=0.0,
        expected_loan_volume=0.0,
        expected_deposit_volume=0.0,
        funding_shortfall=0.0,
    )

    higher_loss_customers = customers.copy()
    higher_loss_customers["expected_loss_rate"] = customers["expected_loss_rate"] + 0.01
    higher_loss_decomposition = ObjectiveDecomposition(
        loan_interest_income=expected_loan_interest_income(
            higher_loss_customers,
            probabilities,
            0.07,
        ),
        expected_credit_loss=expected_credit_loss(higher_loss_customers, probabilities),
        deposit_interest_cost=0.0,
        wholesale_funding_cost=0.0,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=0.0,
        total_objective=0.0,
        expected_loan_volume=0.0,
        expected_deposit_volume=0.0,
        funding_shortfall=0.0,
    )

    assert (
        higher_loss_decomposition.reconciled_total_objective
        < base_decomposition.reconciled_total_objective
    )


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
