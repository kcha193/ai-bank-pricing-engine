"""Cohort accounting for the multi-period roll (FIN-002, Phase 3b).

Every expected value below is worked out by hand from the cohort terms and the period market,
not by calling the implementation under test. Shared market for these tests:

- cash rate 4.00%, wholesale spread 1.00% -> wholesale funding rate 5.00%;
- required deposit funding ratio 65%, shortfall penalty 1.80%;
- RWA weight 0.35, tier-1 ratio 10%, hurdle 12% -> capital charge 0.0042 per dollar of loans.
"""

from __future__ import annotations

import math

import pytest

from balance_pricing_engine.config import BalanceSheetConfig
from balance_pricing_engine.optimization.grid_search import OptimizationResult
from balance_pricing_engine.scenarios.cohort_accounting import (
    BookedCohort,
    evaluate_active_portfolio,
    position_at,
    survival_factor,
)
from balance_pricing_engine.synthetic.market import MarketState

MARKET = MarketState(
    cash_rate=0.04,
    wholesale_funding_spread=0.01,
    competitor_loan_rate=0.065,
    competitor_deposit_rate=0.045,
)
BALANCE_SHEET = BalanceSheetConfig(
    required_deposit_funding_ratio=0.65,
    funding_shortfall_penalty_rate=0.018,
    loan_rwa_weight=0.35,
    capital_hurdle_rate=0.12,
)


def cohort(
    *,
    period: int = 0,
    loan_rate: float = 0.06,
    deposit_rate: float = 0.04,
    loan_volume: float = 1000.0,
    deposit_volume: float = 800.0,
    credit_loss: float = 2.0,
) -> BookedCohort:
    return BookedCohort(
        origination_period=period,
        loan_rate=loan_rate,
        deposit_rate=deposit_rate,
        original_loan_volume=loan_volume,
        original_deposit_volume=deposit_volume,
        booked_credit_loss=credit_loss,
        loan_rwa_weight=0.35,
        capital_hurdle_rate=0.12,
    )


def evaluate(*positions, market: MarketState = MARKET):
    return evaluate_active_portfolio(list(positions), market, BALANCE_SHEET)


# ---------------------------------------------------------------- survival and runoff


def test_t01_t02_zero_runoff_preserves_prior_loan_and_deposit_cohort():
    position = position_at(cohort(), period=1, loan_runoff_rate=0.0, deposit_runoff_rate=0.0)

    assert position.loan_volume == 1000.0
    assert position.deposit_volume == 800.0


def test_t03_loan_runoff_of_one_eliminates_prior_loans_only():
    position = position_at(cohort(), period=1, loan_runoff_rate=1.0, deposit_runoff_rate=0.0)

    assert position.loan_volume == 0.0
    assert position.loan_interest_income == 0.0
    assert position.credit_loss == 0.0
    assert position.capital_charge == 0.0
    assert position.deposit_volume == 800.0


def test_t04_deposit_runoff_of_one_eliminates_prior_deposits_only():
    position = position_at(cohort(), period=1, loan_runoff_rate=0.0, deposit_runoff_rate=1.0)

    assert position.deposit_volume == 0.0
    assert position.deposit_interest_cost == 0.0
    assert position.loan_volume == 1000.0


def test_t05_partial_runoff_declines_geometrically():
    volumes = [
        position_at(cohort(), period=t, loan_runoff_rate=0.25, deposit_runoff_rate=0.5).loan_volume
        for t in range(4)
    ]

    assert volumes == pytest.approx([1000.0, 750.0, 562.5, 421.875])


def test_t06_older_cohorts_survive_by_their_own_vintage():
    older = position_at(cohort(period=0), period=2, loan_runoff_rate=0.5, deposit_runoff_rate=0.5)
    newer = position_at(cohort(period=1), period=2, loan_runoff_rate=0.5, deposit_runoff_rate=0.5)
    fresh = position_at(cohort(period=2), period=2, loan_runoff_rate=0.5, deposit_runoff_rate=0.5)

    assert (older.loan_survival, newer.loan_survival, fresh.loan_survival) == (0.25, 0.5, 1.0)
    assert (older.loan_volume, newer.loan_volume, fresh.loan_volume) == (250.0, 500.0, 1000.0)


def test_t21_loan_and_deposit_runoff_are_independent():
    position = position_at(cohort(), period=2, loan_runoff_rate=0.1, deposit_runoff_rate=0.5)

    assert position.loan_survival == pytest.approx(0.81)
    assert position.deposit_survival == pytest.approx(0.25)
    assert position.loan_volume == pytest.approx(810.0)
    assert position.deposit_volume == pytest.approx(200.0)


@pytest.mark.parametrize("runoff", [-0.01, 1.01, math.nan, math.inf])
def test_t24_invalid_runoff_is_rejected_so_balances_never_go_negative(runoff):
    with pytest.raises(ValueError, match="runoff"):
        survival_factor(runoff, 1)


def test_t24_survival_stays_within_zero_and_one():
    factors = [survival_factor(rate, n) for rate in (0.0, 0.3, 1.0) for n in range(5)]

    assert all(0.0 <= factor <= 1.0 for factor in factors)
    assert survival_factor(1.0, 0) == 1.0  # a cohort contributes in full in its own period


def test_position_before_origination_is_rejected():
    with pytest.raises(ValueError, match="before"):
        position_at(cohort(period=2), period=1, loan_runoff_rate=0.0, deposit_runoff_rate=0.0)


# ---------------------------------------------------------------- contractual cohort terms


def test_t07_t09_t10_t11_surviving_cohort_carries_its_booked_economics():
    position = position_at(cohort(), period=1, loan_runoff_rate=0.5, deposit_runoff_rate=0.5)

    assert position.loan_interest_income == pytest.approx(500.0 * 0.06)  # booked loan rate
    assert position.credit_loss == pytest.approx(2.0 * 0.5)  # booked ECL on survivors
    assert position.capital_charge == pytest.approx(500.0 * 0.0042)
    assert position.deposit_interest_cost == pytest.approx(400.0 * 0.04)  # booked deposit rate


def test_t08_market_rate_move_does_not_reprice_a_surviving_contracted_loan():
    position = position_at(cohort(), period=1, loan_runoff_rate=0.0, deposit_runoff_rate=0.0)
    high_market = MarketState(
        cash_rate=0.10,
        wholesale_funding_spread=0.01,
        competitor_loan_rate=0.12,
        competitor_deposit_rate=0.09,
    )

    economics = evaluate(position, market=high_market)

    assert economics.loan_interest_income == pytest.approx(1000.0 * 0.06)
    assert economics.deposit_interest_cost == pytest.approx(800.0 * 0.04)
    # Only the period funding terms move with the market: 200 wholesale-funded at 11%.
    assert economics.wholesale_funding_cost == pytest.approx(200.0 * 0.11)


def test_t22_t23_cohorts_keep_distinct_booked_rates():
    a = position_at(cohort(period=0), period=1, loan_runoff_rate=0.0, deposit_runoff_rate=0.0)
    b = position_at(
        cohort(
            period=1, loan_rate=0.07, deposit_rate=0.03, loan_volume=500.0, deposit_volume=100.0
        ),
        period=1,
        loan_runoff_rate=0.0,
        deposit_runoff_rate=0.0,
    )

    economics = evaluate(a, b)

    assert economics.loan_interest_income == pytest.approx(1000.0 * 0.06 + 500.0 * 0.07)
    assert economics.deposit_interest_cost == pytest.approx(800.0 * 0.04 + 100.0 * 0.03)


def test_t25_zero_loan_volume_cohort_has_no_loan_economics_and_no_division():
    position = position_at(
        cohort(loan_volume=0.0, credit_loss=0.0, deposit_volume=1000.0),
        period=1,
        loan_runoff_rate=0.3,
        deposit_runoff_rate=0.0,
    )

    assert position.loan_interest_income == 0.0
    assert position.credit_loss == 0.0
    assert position.capital_charge == 0.0


def test_t26_zero_deposit_volume_cohort_is_fully_wholesale_funded():
    economics = evaluate(
        position_at(
            cohort(deposit_volume=0.0), period=0, loan_runoff_rate=0.0, deposit_runoff_rate=0.0
        )
    )

    assert economics.deposit_interest_cost == 0.0
    assert economics.wholesale_funding_cost == pytest.approx(1000.0 * 0.05)
    assert economics.funding_shortfall == pytest.approx(650.0)
    assert economics.funding_shortfall_penalty == pytest.approx(650.0 * 0.018)


# ---------------------------------------------------------------- portfolio funding


def _a_and_b():
    a = position_at(cohort(period=0), period=1, loan_runoff_rate=0.0, deposit_runoff_rate=0.0)
    b = position_at(
        cohort(
            period=1,
            loan_rate=0.07,
            deposit_rate=0.03,
            loan_volume=500.0,
            deposit_volume=100.0,
            credit_loss=1.0,
        ),
        period=1,
        loan_runoff_rate=0.0,
        deposit_runoff_rate=0.0,
    )
    return a, b


def test_t12_wholesale_funding_uses_total_active_volumes():
    economics = evaluate(*_a_and_b())

    assert economics.loan_volume == pytest.approx(1500.0)
    assert economics.deposit_volume == pytest.approx(900.0)
    assert economics.wholesale_funding_cost == pytest.approx(600.0 * 0.05)


def test_t13_shortfall_penalty_uses_total_active_volumes_not_cohort_sum():
    economics = evaluate(*_a_and_b())

    # Portfolio: 65% x 1500 - 900 = 75. Cohort by cohort it would be 0 + (325 - 100) = 225.
    assert economics.funding_shortfall == pytest.approx(75.0)
    assert economics.funding_shortfall_penalty == pytest.approx(75.0 * 0.018)


def test_t14_t15_excess_deposit_income_once_and_no_double_counted_funding_benefit():
    deposit_rich = position_at(
        cohort(
            period=0, loan_volume=0.0, credit_loss=0.0, deposit_volume=1000.0, deposit_rate=0.03
        ),
        period=1,
        loan_runoff_rate=0.0,
        deposit_runoff_rate=0.0,
    )
    loan_heavy = position_at(
        cohort(period=1), period=1, loan_runoff_rate=0.0, deposit_runoff_rate=0.0
    )

    economics = evaluate(deposit_rich, loan_heavy)
    standalone_funding = sum(
        e.excess_deposit_income - e.wholesale_funding_cost - e.funding_shortfall_penalty
        for e in (evaluate(deposit_rich), evaluate(loan_heavy))
    )

    # Portfolio: loans 1000, deposits 1800 -> 800 surplus earns cash once; nothing wholesale.
    assert economics.excess_deposit_income == pytest.approx(800.0 * 0.04)
    assert economics.wholesale_funding_cost == 0.0
    assert economics.funding_shortfall_penalty == 0.0
    # Summing standalone cohorts would give 40 - 10 = 30, not the portfolio's 32.
    assert standalone_funding == pytest.approx(30.0)


def test_t16_period_decomposition_reconciles():
    a, b = _a_and_b()

    economics = evaluate(a, b)

    # 95 - 3 - 6.3 - 35 - 30 - 1.35 + 0
    assert economics.loan_interest_income == pytest.approx(95.0)
    assert economics.expected_credit_loss == pytest.approx(3.0)
    assert economics.capital_charge == pytest.approx(1500.0 * 0.0042)
    assert economics.deposit_interest_cost == pytest.approx(35.0)
    assert economics.total_objective == pytest.approx(95.0 - 3.0 - 6.3 - 35.0 - 30.0 - 1.35)
    assert economics.total_objective == pytest.approx(
        economics.loan_interest_income
        - economics.expected_credit_loss
        - economics.capital_charge
        - economics.deposit_interest_cost
        - economics.wholesale_funding_cost
        - economics.funding_shortfall_penalty
        + economics.excess_deposit_income,
        abs=1e-12,
    )


def test_empty_portfolio_is_zero():
    economics = evaluate()

    assert economics.total_objective == 0.0
    assert economics.loan_volume == 0.0
    assert economics.deposit_volume == 0.0


# ---------------------------------------------------------------- booking from a result


def _result(**overrides) -> OptimizationResult:
    # Cohort A at MARKET: 60 - 2 - 4.2 - 32 - 10 - 0 + 0 = 11.8.
    values = dict(
        loan_rate=0.06,
        deposit_rate=0.04,
        loan_interest_income=60.0,
        expected_credit_loss=2.0,
        capital_charge=4.2,
        deposit_interest_cost=32.0,
        wholesale_funding_cost=10.0,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=0.0,
        total_objective=11.8,
        expected_loan_volume=1000.0,
        expected_deposit_volume=800.0,
        funding_shortfall=0.0,
    )
    values.update(overrides)
    return OptimizationResult(**values)


def test_t17_single_cohort_matches_the_single_period_objective():
    booked = BookedCohort.from_result(
        _result(), origination_period=0, loan_rwa_weight=0.35, capital_hurdle_rate=0.12
    )

    economics = evaluate(
        position_at(booked, period=0, loan_runoff_rate=0.3, deposit_runoff_rate=0.5)
    )

    assert economics.total_objective == pytest.approx(11.8)
    for name in (
        "loan_interest_income",
        "expected_credit_loss",
        "capital_charge",
        "deposit_interest_cost",
        "wholesale_funding_cost",
        "excess_deposit_income",
        "funding_shortfall_penalty",
        "funding_shortfall",
    ):
        assert getattr(economics, name) == pytest.approx(getattr(_result(), name)), name


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"loan_interest_income": 61.0}, "loan interest income"),
        ({"deposit_interest_cost": 30.0}, "deposit interest cost"),
        ({"capital_charge": 5.0}, "capital charge"),
        ({"expected_loan_volume": -1.0}, "non-negative"),
        ({"expected_credit_loss": -1.0}, "non-negative"),
        ({"loan_rate": math.nan}, "finite"),
    ],
)
def test_untraceable_booked_result_is_rejected(overrides, message):
    with pytest.raises(ValueError, match=message):
        BookedCohort.from_result(
            _result(**overrides),
            origination_period=0,
            loan_rwa_weight=0.35,
            capital_hurdle_rate=0.12,
        )


def test_booked_cohort_rejects_negative_origination_period():
    with pytest.raises(ValueError, match="origination_period"):
        cohort(period=-1)


def test_credit_loss_without_booked_loans_is_rejected():
    with pytest.raises(ValueError, match="no loans were booked"):
        cohort(loan_volume=0.0, credit_loss=1.0)


def test_negative_elapsed_periods_are_rejected():
    with pytest.raises(ValueError, match="periods_elapsed"):
        survival_factor(0.3, -1)
