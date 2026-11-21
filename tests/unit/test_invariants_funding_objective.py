"""TEST-002: funding conservation (F01-F13) and objective reconciliation (O01-O10).

Expected values come from ``economic_strategies.reference_portfolio_terms`` and the
reference response functions: plain restatements of ``docs/modelling_assumptions.md`` that
never call the production pricing modules.

Sensitivity tests here are *fixed-candidate* effects (the same rate pair re-evaluated).
They never assume the optimiser's selected rate moves monotonically with a parameter.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from economic_strategies import (
    FundingScenario,
    budget,
    deposit_customers,
    deposit_elasticities,
    funding_scenarios,
    loan_customers,
    loan_elasticities,
    money_close,
    penalty_rates,
    positive_scale,
    rates,
    reference_deposit_probability,
    reference_loan_probability,
    reference_portfolio_terms,
    volumes,
)
from hypothesis import given
from hypothesis import strategies as st

from balance_pricing_engine.config import ResponseCoefficientsConfig
from balance_pricing_engine.optimization.grid_search import (
    evaluate_joint_rate_grid,
    select_best_result,
)
from balance_pricing_engine.optimization.segmented import (
    PortfolioParameters,
    TierResult,
    evaluate_segmented_portfolio,
)
from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.profit import (
    expected_excess_deposit_income,
    expected_wholesale_funding_cost,
)
from balance_pricing_engine.synthetic.market import MarketState

pytestmark = pytest.mark.economic_invariants

_rate = st.floats(0.0, 0.2, allow_nan=False)
_ratio = st.floats(0.0, 1.0, allow_nan=False)


def _funding(loan, deposit, ratio, penalty, wholesale, cash):
    result = calculate_funding_result(loan, deposit, ratio, penalty)
    return {
        "wholesale": expected_wholesale_funding_cost(loan, deposit, wholesale),
        "excess": expected_excess_deposit_income(loan, deposit, cash),
        "required": result.required_deposits,
        "shortfall": result.funding_shortfall,
        "penalty": result.funding_penalty,
    }


# ----------------------------------------------------------------- F01-F10, F13


@given(
    loan=volumes,
    deposit=volumes,
    ratio=_ratio,
    penalty=penalty_rates,
    wholesale=_rate,
    cash=_rate,
)
@budget(300)
def test_f01_to_f06_funding_terms_match_hand_formulas_and_signs(
    loan, deposit, ratio, penalty, wholesale, cash
):
    """F01-F06: every funding term equals its documented piecewise formula and sign."""
    got = _funding(loan, deposit, ratio, penalty, wholesale, cash)
    required = loan * ratio
    expected_shortfall = max(required - deposit, 0.0)
    assert got["wholesale"] >= 0.0  # F01
    assert got["excess"] >= 0.0  # F02 (cash rate >= 0)
    assert got["shortfall"] >= 0.0  # F05
    assert money_close(got["wholesale"], max(loan - deposit, 0.0) * wholesale)
    assert money_close(got["excess"], max(deposit - loan, 0.0) * cash)
    assert money_close(got["required"], required)
    assert money_close(got["shortfall"], expected_shortfall)
    assert money_close(got["penalty"], expected_shortfall * penalty)
    if deposit >= loan:
        assert got["wholesale"] == 0.0  # F03
    if deposit <= loan:
        assert got["excess"] == 0.0  # F04
    if deposit >= required:
        assert got["shortfall"] == 0.0 and got["penalty"] == 0.0  # F06


@given(
    loan=volumes,
    deposit=volumes,
    extra=volumes,
    ratio=_ratio,
    penalty_low=penalty_rates,
    penalty_step=penalty_rates,
)
@budget(200)
def test_f07_f08_shortfall_monotone_in_deposits_and_penalty_monotone_in_rate(
    loan, deposit, extra, ratio, penalty_low, penalty_step
):
    """F07: more deposits never raise the shortfall. F08: a higher penalty rate never
    lowers the penalty for the same L and D."""
    base = calculate_funding_result(loan, deposit, ratio, penalty_low)
    more_deposits = calculate_funding_result(loan, deposit + extra, ratio, penalty_low)
    dearer = calculate_funding_result(loan, deposit, ratio, penalty_low + penalty_step)
    assert more_deposits.funding_shortfall <= base.funding_shortfall
    assert dearer.funding_penalty >= base.funding_penalty


@given(loan=volumes, deposit=volumes, ratio=_ratio, penalty=penalty_rates)
@budget(100)
def test_f09_f10_zero_ratio_or_zero_penalty_rate_charges_nothing(loan, deposit, ratio, penalty):
    assert calculate_funding_result(loan, deposit, 0.0, penalty).funding_penalty == 0.0  # F09
    zero_rate = calculate_funding_result(loan, deposit, ratio, 0.0)
    assert zero_rate.funding_penalty == 0.0  # F10, even when a shortfall exists
    assert zero_rate.funding_shortfall == max(loan * ratio - deposit, 0.0)


@given(
    loan=volumes,
    deposit=volumes,
    scale=positive_scale,
    ratio=_ratio,
    penalty=penalty_rates,
    wholesale=_rate,
    cash=_rate,
)
@budget(200)
def test_f13_funding_terms_are_positively_homogeneous(
    loan, deposit, scale, ratio, penalty, wholesale, cash
):
    """F13: scaling L and D by k > 0 scales every funding term by k."""
    base = _funding(loan, deposit, ratio, penalty, wholesale, cash)
    scaled = _funding(loan * scale, deposit * scale, ratio, penalty, wholesale, cash)
    for name, value in base.items():
        assert math.isclose(scaled[name], value * scale, rel_tol=1e-9, abs_tol=1e-3), name


# ------------------------------------------------------- F11, F12 piecewise boundaries


@pytest.mark.parametrize("loan", [1.0, 1e6, 3.7e9])
@pytest.mark.parametrize("ratio", [0.0, 0.35, 0.65, 1.0])
def test_f11_boundary_values_at_required_deposits_and_at_balanced_book(loan, ratio):
    """F11: at D = L x ratio the shortfall is exactly zero; at D = L no wholesale funding
    and no excess income."""
    at_required = calculate_funding_result(loan, loan * ratio, ratio, 0.05)
    assert at_required.funding_shortfall == 0.0
    assert at_required.funding_penalty == 0.0
    assert expected_wholesale_funding_cost(loan, loan, 0.05) == 0.0
    assert expected_excess_deposit_income(loan, loan, 0.03) == 0.0


@given(
    loan=st.floats(1.0, 5e9),
    ratio=st.floats(0.01, 1.0),
    epsilon_fraction=st.floats(1e-9, 1e-4),
    penalty=st.floats(0.0, 0.3),
    wholesale=_rate,
    cash=_rate,
)
@budget(150)
def test_f12_funding_terms_are_continuous_at_their_kinks(
    loan, ratio, epsilon_fraction, penalty, wholesale, cash
):
    """F12: moving D by epsilon across either kink moves each term by at most rate x epsilon."""
    epsilon = loan * epsilon_fraction
    for kink in (loan * ratio, loan):
        below = _funding(loan, kink - epsilon, ratio, penalty, wholesale, cash)
        above = _funding(loan, kink + epsilon, ratio, penalty, wholesale, cash)
        bound = 2 * epsilon * max(penalty, wholesale, cash, 1e-12) * (1 + 1e-9) + 1e-6
        for name in ("wholesale", "excess", "penalty"):
            assert abs(above[name] - below[name]) <= bound, (name, kink)


# ------------------------------------------------- O01, O07-O10 on the joint grid


@st.composite
def _priced_problem(draw, max_customers: int = 15):
    loans = draw(loan_customers(max_size=max_customers))
    deposits = draw(deposit_customers(max_size=max_customers))
    scenario = draw(funding_scenarios())
    market = MarketState(
        cash_rate=scenario.cash_rate,
        wholesale_funding_spread=scenario.wholesale_funding_rate - scenario.cash_rate,
        competitor_loan_rate=draw(rates),
        competitor_deposit_rate=draw(rates),
    )
    scenario = FundingScenario(
        required_deposit_funding_ratio=scenario.required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=scenario.funding_shortfall_penalty_rate,
        wholesale_funding_rate=market.wholesale_funding_rate,
        cash_rate=market.cash_rate,
        loan_rwa_weight=scenario.loan_rwa_weight,
        capital_hurdle_rate=scenario.capital_hurdle_rate,
    )
    loan_grid = np.array(sorted(set(draw(st.lists(rates, min_size=1, max_size=3)))))
    deposit_grid = np.array(sorted(set(draw(st.lists(rates, min_size=1, max_size=3)))))
    elasticities = {
        "loan_rate_elasticity": draw(loan_elasticities),
        "loan_competitor_gap_elasticity": draw(loan_elasticities),
        "deposit_rate_elasticity": draw(deposit_elasticities),
        "deposit_competitor_gap_elasticity": draw(deposit_elasticities),
    }
    return loans, deposits, market, scenario, loan_grid, deposit_grid, elasticities


def _grid(problem, loans=None, deposits=None):
    base_loans, base_deposits, market, scenario, loan_grid, deposit_grid, elasticities = problem
    return evaluate_joint_rate_grid(
        base_loans if loans is None else loans,
        base_deposits if deposits is None else deposits,
        market,
        loan_grid,
        deposit_grid,
        required_deposit_funding_ratio=scenario.required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=scenario.funding_shortfall_penalty_rate,
        rwa_weight=scenario.loan_rwa_weight,
        capital_hurdle_rate=scenario.capital_hurdle_rate,
        response_coefficients=ResponseCoefficientsConfig(),
        **elasticities,
    )


_MONEY_COLUMNS = (
    "loan_interest_income",
    "expected_credit_loss",
    "capital_charge",
    "deposit_interest_cost",
    "wholesale_funding_cost",
    "excess_deposit_income",
    "funding_shortfall",
    "funding_shortfall_penalty",
    "total_objective",
)


@given(problem=_priced_problem())
@budget(80)
def test_o01_o10_joint_grid_matches_independent_end_to_end_objective(problem):
    """O01 + O10: every grid row equals a from-scratch calculation (reference response,
    reference aggregation, reference funding and capital) to 1e-9 relative."""
    loans, deposits, market, scenario, _, _, elasticities = problem
    coef = ResponseCoefficientsConfig()
    grid = _grid(problem)
    for row in grid.itertuples(index=False):
        p_loan = reference_loan_probability(
            loans,
            row.loan_rate,
            market.competitor_loan_rate,
            elasticities["loan_rate_elasticity"],
            elasticities["loan_competitor_gap_elasticity"],
            coef.loan,
        )
        p_deposit = reference_deposit_probability(
            deposits,
            row.deposit_rate,
            market.competitor_deposit_rate,
            elasticities["deposit_rate_elasticity"],
            elasticities["deposit_competitor_gap_elasticity"],
            coef.deposit,
        )
        loan_balances = loans["loan_amount"].to_numpy()
        deposit_balances = deposits["deposit_amount"].to_numpy()
        loan_volume = float(np.sum(p_loan * loan_balances))
        deposit_volume = float(np.sum(p_deposit * deposit_balances))
        expected = reference_portfolio_terms(
            loan_volume=loan_volume,
            deposit_volume=deposit_volume,
            loan_interest_income=float(np.sum(p_loan * loan_balances * row.loan_rate)),
            credit_loss=float(
                np.sum(p_loan * loan_balances * loans["expected_loss_rate"].to_numpy())
            ),
            deposit_interest_cost=float(np.sum(p_deposit * deposit_balances * row.deposit_rate)),
            scenario=scenario,
        )
        assert money_close(row.expected_loan_volume, loan_volume)
        assert money_close(row.expected_deposit_volume, deposit_volume)
        for column in _MONEY_COLUMNS:
            assert money_close(getattr(row, column), getattr(expected, column)), column
        assert math.isclose(row.return_on_equity, expected.return_on_equity, rel_tol=1e-9,
                            abs_tol=1e-12)


def _zero_balances(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    return frame.assign(**{column: 0.0})


@given(problem=_priced_problem())
@budget(40)
def test_o07_zero_volume_portfolio_invents_no_income_or_funding_cost(problem):
    loans, deposits = problem[0], problem[1]
    grid = _grid(
        problem,
        loans=_zero_balances(loans, "loan_amount"),
        deposits=_zero_balances(deposits, "deposit_amount"),
    )
    for column in (*_MONEY_COLUMNS, "expected_loan_volume", "expected_deposit_volume"):
        assert (grid[column] == 0.0).all(), column
    assert (grid["return_on_equity"] == 0.0).all()


def _best_objective(grid: pd.DataFrame) -> float:
    return float(select_best_result(grid).total_objective)


@given(problem=_priced_problem(max_customers=10), copies=st.integers(2, 4))
@budget(50)
def test_o08_replicating_the_population_scales_every_term_and_keeps_the_optimum(
    problem, copies
):
    """O08: k copies of every customer => volumes and every money term x k (the objective is
    positively homogeneous in the population), ROE unchanged, and the original winner is
    still optimal."""
    loans, deposits = problem[0], problem[1]
    grid = _grid(problem)
    replicated = _grid(
        problem,
        loans=pd.concat([loans] * copies, ignore_index=True),
        deposits=pd.concat([deposits] * copies, ignore_index=True),
    )
    for column in (*_MONEY_COLUMNS, "expected_loan_volume", "expected_deposit_volume"):
        np.testing.assert_allclose(replicated[column], grid[column] * copies, rtol=1e-9, atol=1e-6)
    np.testing.assert_allclose(replicated["return_on_equity"], grid["return_on_equity"],
                               rtol=1e-9, atol=1e-12)
    assert money_close(_best_objective(replicated), _best_objective(grid) * copies)


@given(problem=_priced_problem(), seed=st.integers(0, 2**32 - 1))
@budget(50)
def test_o09_customer_row_order_does_not_change_economics(problem, seed):
    """O09: permuting customer rows changes only float summation order."""
    loans, deposits = problem[0], problem[1]
    rng = np.random.default_rng(seed)
    grid = _grid(problem)
    shuffled = _grid(
        problem,
        loans=loans.iloc[rng.permutation(len(loans))].reset_index(drop=True),
        deposits=deposits.iloc[rng.permutation(len(deposits))].reset_index(drop=True),
    )
    for column in (*_MONEY_COLUMNS, "expected_loan_volume", "expected_deposit_volume"):
        np.testing.assert_allclose(shuffled[column], grid[column], rtol=1e-9, atol=1e-6)
    assert money_close(_best_objective(shuffled), _best_objective(grid))


# ------------------------------------------------- O02, O04-O06 fixed candidates


@st.composite
def _tier_choices(draw):
    def tiers(prefix: str, loan: bool):
        out = []
        for i in range(draw(st.integers(1, 3))):
            volume = draw(volumes)
            rate = draw(rates)
            out.append(
                TierResult(
                    tier_name=f"{prefix}{i}",
                    rate=rate,
                    volume=volume,
                    interest_income=rate * volume if loan else 0.0,
                    credit_loss=volume * draw(st.floats(0.0, 0.03)) if loan else 0.0,
                    interest_cost=0.0 if loan else rate * volume,
                )
            )
        return out

    return tiers("loan", True), tiers("deposit", False), draw(funding_scenarios())


def _params(scenario: FundingScenario, **overrides) -> PortfolioParameters:
    values = {
        "required_deposit_funding_ratio": scenario.required_deposit_funding_ratio,
        "funding_shortfall_penalty_rate": scenario.funding_shortfall_penalty_rate,
        "loan_rwa_weight": scenario.loan_rwa_weight,
        "capital_hurdle_rate": scenario.capital_hurdle_rate,
        "wholesale_funding_rate": scenario.wholesale_funding_rate,
        "cash_rate": scenario.cash_rate,
    }
    values.update(overrides)
    return PortfolioParameters(**values)


@given(choices=_tier_choices())
@budget(200)
def test_o02_segmented_portfolio_reconciles_to_tier_sums_and_reference(choices):
    """O02: totals are the tier sums and the objective equals the hand-built objective."""
    loan_tiers, deposit_tiers, scenario = choices
    result = evaluate_segmented_portfolio(loan_tiers, deposit_tiers, _params(scenario))
    loan_volume = sum(t.volume for t in loan_tiers)
    deposit_volume = sum(t.volume for t in deposit_tiers)
    expected = reference_portfolio_terms(
        loan_volume=loan_volume,
        deposit_volume=deposit_volume,
        loan_interest_income=sum(t.interest_income for t in loan_tiers),
        credit_loss=sum(t.credit_loss for t in loan_tiers),
        deposit_interest_cost=sum(t.interest_cost for t in deposit_tiers),
        scenario=scenario,
    )
    assert money_close(result.total_loan_volume, loan_volume)
    assert money_close(result.total_deposit_volume, deposit_volume)
    for ours, theirs in (
        ("total_objective", "total_objective"),
        ("capital_charge", "capital_charge"),
        ("wholesale_funding_cost", "wholesale_funding_cost"),
        ("excess_deposit_income", "excess_deposit_income"),
        ("funding_shortfall", "funding_shortfall"),
        ("funding_shortfall_penalty", "funding_shortfall_penalty"),
    ):
        assert money_close(getattr(result, ours), getattr(expected, theirs)), ours
    # Tier capital charges sum to the portfolio capital charge (capital is linear in volume).
    assert money_close(sum(t.capital_charge for t in result.loan_tiers), result.capital_charge)


@given(choices=_tier_choices(), extra_loss=volumes)
@budget(150)
def test_o04_extra_ecl_lowers_a_fixed_candidate_objective_one_for_one(choices, extra_loss):
    loan_tiers, deposit_tiers, scenario = choices
    params = _params(scenario)
    base = evaluate_segmented_portfolio(loan_tiers, deposit_tiers, params)
    bumped_tier = TierResult(**{**vars(loan_tiers[0]),
                                "credit_loss": loan_tiers[0].credit_loss + extra_loss})
    bumped = evaluate_segmented_portfolio([bumped_tier, *loan_tiers[1:]], deposit_tiers, params)
    assert money_close(base.total_objective - bumped.total_objective, extra_loss, rel=1e-8)


@given(choices=_tier_choices(), hurdle_step=st.floats(0.0, 0.25))
@budget(150)
def test_o05_higher_capital_charge_lowers_a_fixed_candidate_objective_by_that_charge(
    choices, hurdle_step
):
    loan_tiers, deposit_tiers, scenario = choices
    base = evaluate_segmented_portfolio(loan_tiers, deposit_tiers, _params(scenario))
    dearer = evaluate_segmented_portfolio(
        loan_tiers,
        deposit_tiers,
        _params(scenario, capital_hurdle_rate=scenario.capital_hurdle_rate + hurdle_step),
    )
    extra_charge = dearer.capital_charge - base.capital_charge
    assert extra_charge >= 0.0
    assert money_close(base.total_objective - dearer.total_objective, extra_charge, rel=1e-8)


@given(choices=_tier_choices(), penalty_step=penalty_rates)
@budget(150)
def test_o06_higher_penalty_lowers_a_fixed_candidate_objective_by_the_extra_penalty(
    choices, penalty_step
):
    loan_tiers, deposit_tiers, scenario = choices
    base = evaluate_segmented_portfolio(loan_tiers, deposit_tiers, _params(scenario))
    stricter = evaluate_segmented_portfolio(
        loan_tiers,
        deposit_tiers,
        _params(
            scenario,
            funding_shortfall_penalty_rate=scenario.funding_shortfall_penalty_rate
            + penalty_step,
        ),
    )
    extra_penalty = stricter.funding_shortfall_penalty - base.funding_shortfall_penalty
    assert money_close(extra_penalty, base.funding_shortfall * penalty_step, rel=1e-8)
    assert money_close(base.total_objective - stricter.total_objective, extra_penalty, rel=1e-8)
