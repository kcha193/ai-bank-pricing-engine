"""OPT-002: the segmented optimiser must maximise the full portfolio objective.

The tier-wise optimiser it replaces chose each loan tier assuming wholesale funding and
no shortfall penalty, then chose deposit tiers greedily against the loans already
chosen. These tests pin the joint behaviour: single-tier equivalence with the joint
grid search, exhaustive-search agreement on small grids, coordinate-descent safety on
large ones, the uniform-price safeguard, ROE-floor selection and the artifact contract.
"""

from __future__ import annotations

import json
from itertools import product

import numpy as np
import pandas as pd
import pytest

from balance_pricing_engine.config import (
    DepositTierConfig,
    EngineConfig,
    GridConfig,
    LoanTierConfig,
    ProductConfig,
    SegmentedPricingConfig,
    load_config,
)
from balance_pricing_engine.governance.gates import GateStatus, evaluate_gates
from balance_pricing_engine.optimization.grid_search import GRID_RESULT_COLUMNS
from balance_pricing_engine.optimization.segmented import (
    DepositTierCandidates,
    LoanTierCandidates,
    PortfolioParameters,
    SolverSettings,
    TierResult,
    evaluate_segmented_portfolio,
    optimize_segmented_rates,
    precompute_deposit_tier_candidates,
    precompute_loan_tier_candidates,
    run_coordinate_descent,
    solve_segmented_portfolio,
)
from balance_pricing_engine.pricing.balance_sheet import calculate_funding_result
from balance_pricing_engine.pricing.profit import (
    expected_capital_charge,
    expected_excess_deposit_income,
    expected_wholesale_funding_cost,
)
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.synthetic.market import MarketState

EXACT = SolverSettings(method="exact")
DESCENT = SolverSettings(method="coordinate_descent")


# --------------------------------------------------------------------------- fixtures


def _params(**overrides) -> PortfolioParameters:
    values = {
        "required_deposit_funding_ratio": 0.65,
        "funding_shortfall_penalty_rate": 0.05,
        "loan_rwa_weight": 0.35,
        "capital_hurdle_rate": 0.12,
        "wholesale_funding_rate": 0.05,
        "cash_rate": 0.03,
    }
    values.update(overrides)
    return PortfolioParameters(**values)


def _loan(name: str, rates, volumes, ecl_rate: float = 0.005) -> LoanTierCandidates:
    rates = np.asarray(rates, dtype=float)
    volumes = np.asarray(volumes, dtype=float)
    return LoanTierCandidates(
        tier_name=name,
        rates=rates,
        volume=volumes,
        interest_income=rates * volumes,
        credit_loss=volumes * ecl_rate,
    )


def _deposit(name: str, rates, volumes) -> DepositTierCandidates:
    rates = np.asarray(rates, dtype=float)
    volumes = np.asarray(volumes, dtype=float)
    return DepositTierCandidates(
        tier_name=name,
        rates=rates,
        volume=volumes,
        interest_cost=rates * volumes,
    )


def _customer_tiers(seed: int = 3, n: int = 400, shared: bool = False):
    """Two loan and two deposit tiers of real synthetic customers, three rates each.

    By default the two tiers on each side have disjoint grids (no uniform price is
    feasible); ``shared`` gives both tiers on each side the same grid.
    """
    rng = np.random.default_rng(seed)
    loans = generate_loan_customers(n, rng)
    deposits = generate_deposit_customers(n, rng)
    config = load_config("configs/segmented_pricing.yaml")
    market = MarketState(**config.market.model_dump())
    low_lvr = loans[loans["lvr"] < 0.80]
    high_lvr = loans[loans["lvr"] >= 0.80]
    small = deposits[deposits["deposit_amount"] < 100_000]
    large = deposits[deposits["deposit_amount"] >= 100_000]
    common = {
        "market": market,
        "response_coefficients": config.response.coefficients,
    }
    loan_tiers = [
        precompute_loan_tier_candidates(
            customers,
            tier_name=name,
            rates=np.array(rates),
            loan_rate_elasticity=config.response.loan_rate_elasticity,
            loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
            **common,
        )
        for name, customers, rates in (
            ("low_lvr", low_lvr, [0.050, 0.060, 0.070]),
            ("high_lvr", high_lvr, [0.050, 0.060, 0.070] if shared else [0.055, 0.065, 0.075]),
        )
    ]
    deposit_tiers = [
        precompute_deposit_tier_candidates(
            customers,
            tier_name=name,
            rates=np.array(rates),
            deposit_rate_elasticity=config.response.deposit_rate_elasticity,
            deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
            **common,
        )
        for name, customers, rates in (
            ("small", small, [0.030, 0.040, 0.050]),
            ("large", large, [0.030, 0.040, 0.050] if shared else [0.032, 0.042, 0.052]),
        )
    ]
    params = PortfolioParameters.from_market(
        market,
        required_deposit_funding_ratio=0.65,
        funding_shortfall_penalty_rate=0.018,
        loan_rwa_weight=0.35,
        capital_hurdle_rate=0.12,
    )
    return loan_tiers, deposit_tiers, params


def _exhaustive_reference(loan_tiers, deposit_tiers, params, min_return_on_equity=None):
    """Independent brute force: canonical scalar evaluation of every tier combination."""
    best = None
    best_key = None
    for loan_choice in product(*(range(len(t.rates)) for t in loan_tiers)):
        for deposit_choice in product(*(range(len(t.rates)) for t in deposit_tiers)):
            result = evaluate_segmented_portfolio(
                [t.choice(i) for t, i in zip(loan_tiers, loan_choice)],
                [t.choice(i) for t, i in zip(deposit_tiers, deposit_choice)],
                params,
            )
            qualifies = (
                min_return_on_equity is None
                or result.return_on_equity >= min_return_on_equity
            )
            key = (qualifies, result.total_objective, -result.funding_shortfall)
            if best_key is None or key > best_key:
                best, best_key = (loan_choice, deposit_choice, result), key
    return best


def _base_raw_config() -> dict:
    return {
        "random_seed": 42,
        "simulation": {"n_loan_customers": 300, "n_deposit_customers": 300},
        "market": {
            "cash_rate": 0.025,
            "wholesale_funding_spread": 0.012,
            "competitor_loan_rate": 0.0499,
            "competitor_deposit_rate": 0.039,
            "home_loan_reference_rate": 0.0499,
            "term_deposit_reference_rate": 0.039,
        },
        "balance_sheet": {
            "required_deposit_funding_ratio": 0.65,
            "funding_shortfall_penalty_rate": 0.018,
        },
        "response": {
            "loan_rate_elasticity": -170.0,
            "loan_competitor_gap_elasticity": -130.0,
            "deposit_rate_elasticity": 140.0,
            "deposit_competitor_gap_elasticity": 110.0,
        },
    }


def _single_tier_pair(raw: dict, loan_grid: dict, deposit_grid: dict):
    segmented = dict(raw)
    segmented["segmented_pricing"] = {
        "loan_tiers": [{"name": "all", "rate_grid": loan_grid}],
        "deposit_tiers": [{"name": "all", "rate_grid": deposit_grid}],
    }
    joint = dict(raw)
    joint["products"] = {"loan_rate_grid": loan_grid, "deposit_rate_grid": deposit_grid}
    return EngineConfig.model_validate(segmented), EngineConfig.model_validate(joint)


EQUIVALENCE_FIELDS = (
    "loan_rate",
    "deposit_rate",
    "total_objective",
    "expected_loan_volume",
    "expected_deposit_volume",
    "loan_interest_income",
    "expected_credit_loss",
    "capital_charge",
    "deposit_interest_cost",
    "wholesale_funding_cost",
    "excess_deposit_income",
    "funding_shortfall_penalty",
    "funding_shortfall",
)

EQUIVALENCE_CASES = {
    "base": {},
    "high_penalty": {"funding_shortfall_penalty_rate": 0.20},
    "zero_ratio": {"required_deposit_funding_ratio": 0.0},
    "zero_penalty": {"funding_shortfall_penalty_rate": 0.0},
    "full_ratio": {"required_deposit_funding_ratio": 1.0},
}


# ------------------------------------------------------- T01-T08 single-tier equivalence


@pytest.mark.parametrize("case", sorted(EQUIVALENCE_CASES))
def test_single_tier_segmented_matches_joint_optimiser(case):
    raw = _base_raw_config()
    raw["balance_sheet"] = {**raw["balance_sheet"], **EQUIVALENCE_CASES[case]}
    segmented, joint = _single_tier_pair(
        raw,
        {"start": 0.03, "stop": 0.09, "step": 0.0025},
        {"start": 0.02, "stop": 0.06, "step": 0.0025},
    )

    seg = run_baseline_diagnostics(segmented, max_boundary_expansions=0)
    jnt = run_baseline_diagnostics(joint, max_boundary_expansions=0)

    # Same customers, grid and arithmetic order: every component is bit-identical.
    for field in EQUIVALENCE_FIELDS:
        assert getattr(seg.best_result, field) == getattr(jnt.best_result, field), field
    assert seg.selected_return_on_equity == pytest.approx(jnt.selected_return_on_equity)
    assert seg.segmented_solver["solver_method"] == "EXACT"
    assert seg.segmented_solver["optimality_proven"] is True


def test_single_tier_equivalence_holds_with_roe_floor_between_candidates():
    raw = _base_raw_config()
    segmented, joint = _single_tier_pair(
        raw,
        {"start": 0.03, "stop": 0.09, "step": 0.0025},
        {"start": 0.02, "stop": 0.06, "step": 0.0025},
    )
    unconstrained = run_baseline_diagnostics(joint, max_boundary_expansions=0)
    floor = unconstrained.selected_return_on_equity + 0.05
    with_floor = {
        "balance_sheet": joint.balance_sheet.model_copy(update={"min_return_on_equity": floor})
    }

    seg = run_baseline_diagnostics(segmented.model_copy(update=with_floor), 0)
    jnt = run_baseline_diagnostics(joint.model_copy(update=with_floor), 0)

    assert jnt.roe_constraint_relaxed is False
    assert seg.roe_constraint_relaxed is False
    assert seg.best_result.loan_rate == pytest.approx(jnt.best_result.loan_rate)
    assert seg.best_result.deposit_rate == pytest.approx(jnt.best_result.deposit_rate)
    assert seg.best_result.total_objective == pytest.approx(jnt.best_result.total_objective)
    assert seg.selected_return_on_equity >= floor


# ------------------------------------------------------------ T41 audit reproduction


def test_original_opt002_audit_scenario_matches_joint_optimum():
    base = load_config("configs/base.yaml")
    loan_grid = GridConfig(start=0.03, stop=0.09, step=0.0005)
    deposit_grid = GridConfig(start=0.02, stop=0.06, step=0.0005)
    segmented = base.model_copy(
        update={
            "pricing_strategy": None,
            "segmented_pricing": SegmentedPricingConfig(
                loan_tiers=[LoanTierConfig(name="all", rate_grid=loan_grid)],
                deposit_tiers=[DepositTierConfig(name="all", rate_grid=deposit_grid)],
            ),
        }
    )
    joint = base.model_copy(
        update={
            "pricing_strategy": None,
            "products": ProductConfig(loan_rate_grid=loan_grid, deposit_rate_grid=deposit_grid),
        }
    )

    seg = run_baseline_diagnostics(segmented, max_boundary_expansions=0)
    jnt = run_baseline_diagnostics(joint, max_boundary_expansions=0)

    # November 13 audit: tier-wise result was 5.65% / -$0.27m / $552m shortfall.
    # Same grid, customers and arithmetic: rates and objective are bit-identical.
    assert seg.best_result.loan_rate == jnt.best_result.loan_rate == 0.0615
    assert seg.best_result.deposit_rate == jnt.best_result.deposit_rate == 0.0415
    assert seg.best_result.total_objective == jnt.best_result.total_objective
    assert seg.best_result.funding_shortfall == pytest.approx(jnt.best_result.funding_shortfall)
    assert seg.best_result.total_objective > 0
    assert seg.best_result.funding_shortfall < 0.01 * seg.best_result.expected_loan_volume


# ------------------------------------------------------ T09-T12 funding interaction


def test_high_shortfall_penalty_moves_loan_tier_to_smaller_book():
    loans = [_loan("all", [0.07, 0.08], [1000.0, 500.0])]
    deposits = [_deposit("all", [0.03], [600.0])]
    base = {
        "required_deposit_funding_ratio": 1.0,
        "wholesale_funding_rate": 0.035,
        "cash_rate": 0.03,
    }

    no_penalty = solve_segmented_portfolio(
        loans, deposits, _params(funding_shortfall_penalty_rate=0.0, **base), settings=EXACT
    )
    high_penalty = solve_segmented_portfolio(
        loans, deposits, _params(funding_shortfall_penalty_rate=0.10, **base), settings=EXACT
    )

    assert no_penalty.loan_indices == (0,)
    assert high_penalty.loan_indices == (1,)
    assert high_penalty.result.funding_shortfall == 0.0


def test_required_funding_ratio_changes_candidate_objective_exactly():
    loans = [_loan("all", [0.06], [1000.0])]
    deposits = [_deposit("all", [0.03], [400.0])]

    def objective(ratio: float) -> float:
        return evaluate_segmented_portfolio(
            [loans[0].choice(0)],
            [deposits[0].choice(0)],
            _params(required_deposit_funding_ratio=ratio, funding_shortfall_penalty_rate=0.02),
        ).total_objective

    # Required deposits 650 -> shortfall 250; required 300 -> shortfall 0.
    assert objective(0.65) - objective(0.30) == pytest.approx(-250.0 * 0.02)
    assert objective(0.30) == pytest.approx(objective(0.0))


def test_loan_rate_choice_reacts_to_deposit_volume():
    loans = [_loan("all", [0.05, 0.06], [1000.0, 500.0])]

    rich = solve_segmented_portfolio(
        loans, [_deposit("all", [0.03], [1000.0])], _params(), settings=EXACT
    )
    poor = solve_segmented_portfolio(
        loans, [_deposit("all", [0.03], [100.0])], _params(), settings=EXACT
    )

    assert rich.loan_indices == (0,)
    assert poor.loan_indices == (1,)


def test_deposit_rate_choice_reacts_to_loan_volume():
    deposits = [_deposit("all", [0.02, 0.04], [100.0, 1000.0])]

    large_book = solve_segmented_portfolio(
        [_loan("all", [0.05], [1000.0])], deposits, _params(), settings=EXACT
    )
    small_book = solve_segmented_portfolio(
        [_loan("all", [0.05], [50.0])], deposits, _params(), settings=EXACT
    )

    assert large_book.deposit_indices == (1,)
    assert small_book.deposit_indices == (0,)


# ---------------------------------------------------------- T13-T14 exhaustive search


def test_two_by_two_tier_exact_search_matches_independent_exhaustive_reference():
    loan_tiers, deposit_tiers, params = _customer_tiers()

    solved = solve_segmented_portfolio(loan_tiers, deposit_tiers, params, settings=EXACT)
    loan_choice, deposit_choice, reference = _exhaustive_reference(
        loan_tiers, deposit_tiers, params
    )

    assert solved.diagnostics.search_space_size == 81
    assert solved.diagnostics.candidate_evaluations >= 81
    assert solved.diagnostics.optimality_proven is True
    assert (solved.loan_indices, solved.deposit_indices) == (loan_choice, deposit_choice)
    assert solved.result.total_objective == pytest.approx(reference.total_objective, rel=1e-12)


def test_exact_search_finds_known_global_optimum_that_greedy_tier_choice_misses():
    # Tier-wise view (wholesale-funded, no penalty) prefers the big cheap book in both
    # loan tiers; with scarce deposits the shortfall penalty makes one small tier optimal.
    loans = [
        _loan("a", [0.050, 0.065], [1000.0, 400.0]),
        _loan("b", [0.050, 0.065], [1000.0, 400.0]),
    ]
    deposits = [_deposit("d", [0.03, 0.045], [300.0, 900.0])]
    params = _params(funding_shortfall_penalty_rate=0.08)

    solved = solve_segmented_portfolio(loans, deposits, params, settings=EXACT)
    loan_choice, deposit_choice, reference = _exhaustive_reference(loans, deposits, params)

    assert (solved.loan_indices, solved.deposit_indices) == (loan_choice, deposit_choice)
    assert solved.result.total_objective == pytest.approx(reference.total_objective)
    assert solved.loan_indices != (0, 0)


# ---------------------------------------------------------- T15-T20 coordinate descent


def test_coordinate_descent_accepted_moves_never_decrease_objective():
    loan_tiers, deposit_tiers, params = _customer_tiers()

    run = run_coordinate_descent(
        loan_tiers,
        deposit_tiers,
        params,
        start=((0, 0), (0, 0)),
        settings=DESCENT,
    )

    assert len(run.accepted_objectives) >= 2
    assert all(b > a for a, b in zip(run.accepted_objectives, run.accepted_objectives[1:]))
    assert run.objective == run.accepted_objectives[-1]


def test_coordinate_descent_converges_to_global_optimum_on_separable_problem():
    # Equal wholesale and cash rates with no ratio make funding linear, so the
    # objective separates by tier and one sweep reaches the global optimum.
    loan_tiers, deposit_tiers, _ = _customer_tiers()
    params = _params(
        required_deposit_funding_ratio=0.0,
        funding_shortfall_penalty_rate=0.0,
        wholesale_funding_rate=0.04,
        cash_rate=0.04,
    )

    descent = solve_segmented_portfolio(loan_tiers, deposit_tiers, params, settings=DESCENT)
    exact = solve_segmented_portfolio(loan_tiers, deposit_tiers, params, settings=EXACT)

    assert descent.diagnostics.solver_method == "COORDINATE_DESCENT"
    assert descent.diagnostics.converged is True
    assert descent.diagnostics.optimality_proven is False
    assert descent.result.total_objective == pytest.approx(exact.result.total_objective)


def test_coordinate_descent_under_roe_floor_never_lowers_the_lexicographic_key():
    loans, deposits, params = _roe_fixture()
    small = evaluate_segmented_portfolio([loans[0].choice(1)], [deposits[0].choice(0)], params)
    large = evaluate_segmented_portfolio([loans[0].choice(0)], [deposits[0].choice(0)], params)
    floor = (small.return_on_equity + large.return_on_equity) / 2

    run = run_coordinate_descent(
        loans, deposits, params, start=((0,), (0,)), settings=DESCENT,
        min_return_on_equity=floor,
    )

    # Gaining the floor is accepted at a lower objective; the (meets, objective) key rises.
    assert run.loan_indices == (1,)
    assert run.meets_roe_floor is True
    assert run.accepted_objectives == (large.total_objective, small.total_objective)


def test_coordinate_descent_respects_iteration_cap_and_reports_non_convergence():
    loan_tiers, deposit_tiers, params = _customer_tiers()

    run = run_coordinate_descent(
        loan_tiers,
        deposit_tiers,
        params,
        start=((0, 0), (0, 0)),
        settings=SolverSettings(method="coordinate_descent", max_iterations=1),
    )
    solved = solve_segmented_portfolio(
        loan_tiers,
        deposit_tiers,
        params,
        settings=SolverSettings(method="coordinate_descent", max_iterations=1),
    )

    assert run.iterations == 1
    assert run.converged is False
    assert run.iteration_limit_reached is True
    assert solved.diagnostics.converged is False
    assert solved.diagnostics.iteration_limit_reached is True


def test_multi_start_search_is_deterministic():
    loan_tiers, deposit_tiers, params = _customer_tiers()

    first = solve_segmented_portfolio(loan_tiers, deposit_tiers, params, settings=DESCENT)
    second = solve_segmented_portfolio(loan_tiers, deposit_tiers, params, settings=DESCENT)

    assert first.loan_indices == second.loan_indices
    assert first.deposit_indices == second.deposit_indices
    assert first.diagnostics == second.diagnostics


def test_multi_start_result_is_no_worse_than_any_start():
    loan_tiers, deposit_tiers, params = _customer_tiers()

    solved = solve_segmented_portfolio(loan_tiers, deposit_tiers, params, settings=DESCENT)

    assert 2 <= solved.diagnostics.starts <= DESCENT.max_starts
    assert len(solved.diagnostics.start_objectives) == solved.diagnostics.starts
    for initial, final in solved.diagnostics.start_objectives:
        assert final >= initial
        assert solved.result.total_objective >= final


# ------------------------------------------------------ T21-T23 uniform baseline


def test_uniform_baseline_is_feasible_when_tier_grids_share_rates():
    params = _params()
    loans = [
        _loan("a", [0.05, 0.06], [900.0, 600.0]),
        _loan("b", [0.06, 0.07], [800.0, 500.0]),
    ]
    deposits = [_deposit("d", [0.03, 0.04], [400.0, 900.0])]

    solved = solve_segmented_portfolio(loans, deposits, params, settings=DESCENT)

    # 6.00% is the only loan rate in both tier grids; both deposit rates are uniform.
    uniform_objectives = [
        evaluate_segmented_portfolio(
            [loans[0].choice(1), loans[1].choice(0)], [deposits[0].choice(d)], params
        ).total_objective
        for d in (0, 1)
    ]
    assert solved.diagnostics.uniform_baseline_feasible is True
    assert solved.diagnostics.uniform_baseline_objective == pytest.approx(max(uniform_objectives))


@pytest.mark.parametrize("settings", [EXACT, DESCENT], ids=["exact", "descent"])
def test_segmented_objective_is_never_below_feasible_uniform_baseline(settings):
    loan_tiers, deposit_tiers, params = _customer_tiers(shared=True)

    solved = solve_segmented_portfolio(loan_tiers, deposit_tiers, params, settings=settings)

    assert solved.diagnostics.uniform_baseline_feasible is True
    assert solved.result.total_objective >= solved.diagnostics.uniform_baseline_objective


def test_uniform_baseline_is_not_claimed_when_tier_grids_share_no_rate():
    loan_tiers, deposit_tiers, params = _customer_tiers()  # loan grids are disjoint

    solved = solve_segmented_portfolio(loan_tiers, deposit_tiers, params, settings=DESCENT)

    assert solved.diagnostics.uniform_baseline_feasible is False
    assert solved.diagnostics.uniform_baseline_objective is None


# ------------------------------------------------ T24-T27, T36 portfolio economics


def _decomposition(loan_volume, deposit_volume, params, income=0.0, ecl=0.0, cost=0.0):
    capital = expected_capital_charge(
        loan_volume, params.loan_rwa_weight, params.capital_hurdle_rate
    )
    wholesale = expected_wholesale_funding_cost(
        loan_volume, deposit_volume, params.wholesale_funding_rate
    )
    excess = expected_excess_deposit_income(loan_volume, deposit_volume, params.cash_rate)
    funding = calculate_funding_result(
        loan_volume,
        deposit_volume,
        params.required_deposit_funding_ratio,
        params.funding_shortfall_penalty_rate,
    )
    return income - ecl - capital - cost - wholesale - funding.funding_penalty + excess


def test_portfolio_objective_equals_independent_decomposition():
    loans = [_loan("a", [0.06], [700.0]), _loan("b", [0.07], [300.0])]
    deposits = [_deposit("s", [0.03], [200.0]), _deposit("l", [0.04], [250.0])]
    params = _params()

    result = evaluate_segmented_portfolio(
        [t.choice(0) for t in loans], [t.choice(0) for t in deposits], params
    )

    income = 0.06 * 700 + 0.07 * 300
    ecl = 1000 * 0.005
    cost = 0.03 * 200 + 0.04 * 250
    assert result.total_loan_volume == pytest.approx(1000.0)
    assert result.total_deposit_volume == pytest.approx(450.0)
    assert result.total_objective == pytest.approx(
        _decomposition(1000.0, 450.0, params, income, ecl, cost)
    )
    assert result.total_objective == pytest.approx(
        result.total_loan_interest_income
        - result.total_credit_loss
        - result.capital_charge
        - result.total_deposit_interest_cost
        - result.wholesale_funding_cost
        - result.funding_shortfall_penalty
        + result.excess_deposit_income
    )
    assert result.funding_shortfall == pytest.approx(650.0 - 450.0)


def test_zero_funding_ratio_removes_shortfall_penalty():
    result = evaluate_segmented_portfolio(
        [_loan("a", [0.06], [1000.0]).choice(0)],
        [_deposit("d", [0.03], [100.0]).choice(0)],
        _params(required_deposit_funding_ratio=0.0),
    )

    assert result.funding_shortfall == 0.0
    assert result.funding_shortfall_penalty == 0.0
    assert result.wholesale_funding_cost == pytest.approx(900.0 * 0.05)


def test_zero_penalty_rate_keeps_shortfall_but_charges_nothing():
    result = evaluate_segmented_portfolio(
        [_loan("a", [0.06], [1000.0]).choice(0)],
        [_deposit("d", [0.03], [100.0]).choice(0)],
        _params(funding_shortfall_penalty_rate=0.0),
    )

    assert result.funding_shortfall == pytest.approx(550.0)
    assert result.funding_shortfall_penalty == 0.0


def test_deposits_above_loans_earn_cash_rate_excess_income():
    result = evaluate_segmented_portfolio(
        [_loan("a", [0.06], [400.0]).choice(0)],
        [_deposit("d", [0.03], [1000.0]).choice(0)],
        _params(cash_rate=0.025),
    )

    assert result.excess_deposit_income == pytest.approx(600.0 * 0.025)
    assert result.wholesale_funding_cost == 0.0
    assert result.funding_shortfall == 0.0


def test_zero_volumes_produce_zero_economics_and_zero_roe():
    result = evaluate_segmented_portfolio(
        [_loan("a", [0.06], [0.0]).choice(0)],
        [_deposit("d", [0.03], [0.0]).choice(0)],
        _params(),
    )
    solved = solve_segmented_portfolio(
        [_loan("a", [0.06], [0.0])], [_deposit("d", [0.03], [0.0])], _params(), settings=EXACT
    )

    assert result.total_objective == 0.0
    assert result.return_on_equity == 0.0
    assert solved.result.total_objective == 0.0


# --------------------------------------------------------- T28-T30 ordering and ties


def test_tier_order_does_not_change_optimum():
    loan_tiers, deposit_tiers, params = _customer_tiers()

    forward = solve_segmented_portfolio(loan_tiers, deposit_tiers, params, settings=EXACT)
    reverse = solve_segmented_portfolio(
        loan_tiers[::-1], deposit_tiers[::-1], params, settings=EXACT
    )

    def rates(solution):
        return (
            {t.tier_name: t.rate for t in solution.result.loan_tiers},
            {t.tier_name: t.rate for t in solution.result.deposit_tiers},
        )

    assert rates(forward) == rates(reverse)
    assert forward.result.total_objective == pytest.approx(reverse.result.total_objective)


def test_customer_row_order_does_not_change_segmented_rates():
    config = load_config("configs/segmented_pricing.yaml")
    rng = np.random.default_rng(5)
    loans = generate_loan_customers(300, rng)
    deposits = generate_deposit_customers(300, rng)
    kwargs = _optimizer_kwargs(config)

    ordered = optimize_segmented_rates(loans, deposits, **kwargs)
    shuffled = optimize_segmented_rates(
        loans.sample(frac=1.0, random_state=1), deposits.sample(frac=1.0, random_state=2), **kwargs
    )

    assert [t.rate for t in ordered.loan_tiers] == [t.rate for t in shuffled.loan_tiers]
    assert [t.rate for t in ordered.deposit_tiers] == [t.rate for t in shuffled.deposit_tiers]
    assert ordered.total_objective == pytest.approx(shuffled.total_objective, rel=1e-9)


def test_tier_candidate_grid_includes_both_endpoints():
    config = load_config("configs/segmented_pricing.yaml")
    rng = np.random.default_rng(0)
    result = optimize_segmented_rates(
        generate_loan_customers(200, rng), generate_deposit_customers(200, rng),
        **_optimizer_kwargs(config),
    )

    grid = result.candidate_rates["deposit_tiers"]["wholesale"]
    assert grid[0] == pytest.approx(0.044)
    assert grid[-1] == pytest.approx(0.060)


def test_ties_break_on_lower_shortfall_then_enumeration_order():
    # Both loan candidates give the same objective; the second has no shortfall.
    params = _params(funding_shortfall_penalty_rate=0.0, wholesale_funding_rate=0.0, cash_rate=0.0,
                     capital_hurdle_rate=0.0)
    loans = [
        LoanTierCandidates(
            tier_name="a",
            rates=np.array([0.05, 0.06]),
            volume=np.array([1000.0, 500.0]),
            interest_income=np.array([30.0, 30.0]),
            credit_loss=np.array([0.0, 0.0]),
        )
    ]
    deposits = [_deposit("d", [0.0], [400.0])]
    same = [_loan("a", [0.05, 0.05], [1000.0, 1000.0])]

    solved = solve_segmented_portfolio(loans, deposits, params, settings=EXACT)
    first_wins = solve_segmented_portfolio(same, deposits, params, settings=EXACT)

    assert solved.loan_indices == (1,)
    assert first_wins.loan_indices == (0,)


def test_exact_ties_break_on_distance_to_market_reference_rates():
    # Identical economics at 5% and 6%; the 6% loan rate is nearer the 6.1% reference.
    params = _params(capital_hurdle_rate=0.0)
    loans = [
        LoanTierCandidates(
            tier_name="a",
            rates=np.array([0.05, 0.06]),
            volume=np.array([1000.0, 1000.0]),
            interest_income=np.array([50.0, 50.0]),
            credit_loss=np.array([0.0, 0.0]),
        )
    ]
    deposits = [_deposit("d", [0.03], [1000.0])]

    near_high = solve_segmented_portfolio(
        loans, deposits, params, settings=EXACT,
        home_loan_reference_rate=0.061, term_deposit_reference_rate=0.03,
    )
    near_low = solve_segmented_portfolio(
        loans, deposits, params, settings=EXACT,
        home_loan_reference_rate=0.049, term_deposit_reference_rate=0.03,
    )
    descent = solve_segmented_portfolio(
        loans, deposits, params, settings=DESCENT,
        home_loan_reference_rate=0.061, term_deposit_reference_rate=0.03,
    )

    assert near_high.loan_indices == (1,)
    assert near_low.loan_indices == (0,)
    assert descent.result.total_objective == near_high.result.total_objective
    # Without a deposit reference the distance is undefined (NaN), exactly as in the joint
    # optimiser, so the tie falls through to enumeration order.
    undefined = solve_segmented_portfolio(
        loans, deposits, params, settings=EXACT, home_loan_reference_rate=0.061
    )
    assert undefined.loan_indices == (0,)


def test_uniform_baseline_dominance_survives_start_truncation():
    loan_tiers, deposit_tiers, params = _customer_tiers(shared=True)

    solved = solve_segmented_portfolio(
        loan_tiers,
        deposit_tiers,
        params,
        settings=SolverSettings(method="coordinate_descent", max_starts=1),
        starts=[((0, 0), (1, 0))],  # stalls at a local optimum below uniform
    )

    assert solved.diagnostics.starts == 1
    assert solved.result.total_objective >= solved.diagnostics.uniform_baseline_objective


def test_single_tier_exact_search_does_not_repeat_uniform_enumeration():
    loans = [_loan("a", np.linspace(0.04, 0.07, 7), np.linspace(900.0, 300.0, 7))]
    deposits = [_deposit("d", np.linspace(0.02, 0.05, 5), np.linspace(200.0, 900.0, 5))]

    solved = solve_segmented_portfolio(loans, deposits, _params())

    assert solved.diagnostics.candidate_evaluations == solved.diagnostics.search_space_size == 35
    assert solved.diagnostics.uniform_baseline_objective == solved.result.total_objective


# ---------------------------------------------------------- T31-T33 ROE and governance


def _roe_fixture():
    # Big book: higher objective, low ROE. Small book: lower objective, high ROE.
    loans = [_loan("a", [0.05, 0.08], [10_000.0, 1_000.0])]
    deposits = [_deposit("d", [0.01], [10_000.0])]
    params = _params(required_deposit_funding_ratio=0.0)
    return loans, deposits, params


@pytest.mark.parametrize("settings", [EXACT, DESCENT], ids=["exact", "descent"])
def test_roe_floor_is_respected_when_a_qualifying_candidate_exists(settings):
    loans, deposits, params = _roe_fixture()
    unconstrained = solve_segmented_portfolio(loans, deposits, params, settings=settings)
    small = evaluate_segmented_portfolio([loans[0].choice(1)], [deposits[0].choice(0)], params)
    assert unconstrained.loan_indices == (0,)
    assert small.return_on_equity > unconstrained.result.return_on_equity

    floor = (small.return_on_equity + unconstrained.result.return_on_equity) / 2
    constrained = solve_segmented_portfolio(
        loans, deposits, params, settings=settings, min_return_on_equity=floor
    )

    assert constrained.loan_indices == (1,)
    assert constrained.roe_constraint_relaxed is False
    assert constrained.result.return_on_equity >= floor


@pytest.mark.parametrize("settings", [EXACT, DESCENT], ids=["exact", "descent"])
def test_unreachable_roe_floor_is_relaxed_and_disclosed(settings):
    loans, deposits, params = _roe_fixture()

    solved = solve_segmented_portfolio(
        loans, deposits, params, settings=settings, min_return_on_equity=100.0
    )

    assert solved.roe_constraint_relaxed is True
    assert solved.result.roe_constraint_relaxed is True
    assert solved.loan_indices == (0,)


def test_segmented_runner_honours_roe_floor_and_gate_passes():
    config = EngineConfig.model_validate(_two_tier_raw_config())
    unconstrained = run_baseline_diagnostics(config, max_boundary_expansions=0)
    floor = unconstrained.selected_return_on_equity + 0.02
    constrained_config = config.model_copy(
        update={
            "balance_sheet": config.balance_sheet.model_copy(
                update={"min_return_on_equity": floor}
            )
        }
    )

    constrained = run_baseline_diagnostics(constrained_config, max_boundary_expansions=0)

    assert constrained.roe_constraint_relaxed is False
    assert constrained.selected_return_on_equity >= floor
    assert not any(
        "minimum return on equity" in w for w in constrained.boundary_diagnostics.warnings
    )
    gates = {g.code: g for g in _gates(constrained_config, constrained)}
    assert gates["ROE_FLOOR_MET"].status is GateStatus.PASS


def test_negative_segmented_objective_triggers_objective_positive_warning():
    raw = _two_tier_raw_config()
    raw["market"]["wholesale_funding_spread"] = 0.08
    raw["balance_sheet"]["funding_shortfall_penalty_rate"] = 0.5
    for tier in raw["segmented_pricing"]["loan_tiers"]:
        tier["rate_grid"] = {"start": 0.020, "stop": 0.030, "step": 0.005}
    config = EngineConfig.model_validate(raw)

    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=0)

    assert diagnostics.best_result.total_objective <= 0
    gates = {g.code: g for g in _gates(config, diagnostics)}
    assert gates["OBJECTIVE_POSITIVE"].status is GateStatus.WARN


# ------------------------------------------------------- T34-T35 boundary expansion


def test_boundary_expansion_never_lowers_best_known_objective():
    raw = _two_tier_raw_config()
    for tier in raw["segmented_pricing"]["loan_tiers"]:
        tier["rate_grid"] = {"start": 0.050, "stop": 0.062, "step": 0.005}
    config = EngineConfig.model_validate(raw)

    initial = run_baseline_diagnostics(config, max_boundary_expansions=0)
    expanded = run_baseline_diagnostics(config, max_boundary_expansions=2)

    assert initial.boundary_diagnostics.has_warning
    assert expanded.expansion_rounds >= 1
    assert expanded.best_result.total_objective >= initial.best_result.total_objective


def test_expansion_incumbent_survives_off_lattice_and_is_needed():
    # 5.00-6.20% in 0.5% steps appends 6.20% off-lattice; expanding the stop to 7.40%
    # gives 5.00, 5.50, ..., 7.00, 7.40, so the previous winner (6.20%) leaves the grid.
    from balance_pricing_engine.scenarios.runner import (
        _expand_segmented_config_for_boundary,
        _tier_rates,
    )

    raw = _two_tier_raw_config()
    for tier in raw["segmented_pricing"]["loan_tiers"]:
        tier["rate_grid"] = {"start": 0.050, "stop": 0.062, "step": 0.005}
    config = EngineConfig.model_validate(raw)
    rng = np.random.default_rng(config.random_seed)
    loans, deposits = generate_loan_customers(300, rng), generate_deposit_customers(300, rng)
    kwargs = _optimizer_kwargs(config)
    kwargs.pop("config")

    first = optimize_segmented_rates(loans, deposits, config=config.segmented_pricing, **kwargs)
    expanded, did_expand = _expand_segmented_config_for_boundary(first, config.segmented_pricing)
    without = optimize_segmented_rates(loans, deposits, config=expanded, **kwargs)
    with_incumbent = optimize_segmented_rates(
        loans, deposits, config=expanded, incumbent_tier_rates=_tier_rates(first), **kwargs
    )

    assert did_expand
    assert _tier_rates(first)["loan_tiers"] == {"standard": 0.062, "high_lvr": 0.062}
    assert without.total_objective < first.total_objective
    assert with_incumbent.total_objective >= first.total_objective
    standard_grid = with_incumbent.candidate_rates["loan_tiers"]["standard"]
    assert standard_grid.count(0.062) == 1
    assert 0.062 not in without.candidate_rates["loan_tiers"]["standard"]


def test_expansion_win_outside_initial_band_sets_policy_band_breach():
    raw = _two_tier_raw_config()
    for tier in raw["segmented_pricing"]["loan_tiers"]:
        tier["rate_grid"] = {"start": 0.030, "stop": 0.036, "step": 0.003}
    config = EngineConfig.model_validate(raw)

    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=1)

    assert diagnostics.expansion_rounds == 1
    assert any(r > 0.036 for r in diagnostics.segmented_tier_rates["loan_tiers"].values())
    assert diagnostics.policy_band_breached is True
    assert diagnostics.initial_boundary_diagnostics.has_warning


# ----------------------------------------------------- T37-T40 diagnostics/artifacts


def test_segmented_artifacts_keep_contract_and_add_solver_diagnostics(tmp_path):
    diagnostics = run_baseline_diagnostics(
        EngineConfig.model_validate(_two_tier_raw_config()), max_boundary_expansions=0
    )

    diagnostics.write_artifacts(tmp_path)

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    for key in (
        "best_loan_rate",
        "best_deposit_rate",
        "objective_value",
        "expected_loan_volume",
        "expected_deposit_volume",
        "funding_shortfall",
        "excess_deposit_income",
        "boundary_warning_flag",
        "initial_boundary_warning_flag",
        "expansion_rounds",
        "expansion_capped",
        "policy_band_breached",
        "roe_constraint_relaxed",
        "segmented_tier_rates",
    ):
        assert key in summary
    solver = summary["segmented_solver"]
    assert solver["solver_method"] == "EXACT"
    assert solver["solver_converged"] is True
    assert solver["optimality_proven"] is True
    assert solver["solver_starts"] == 0
    assert solver["solver_iterations"] == 0
    assert solver["candidate_evaluations"] >= solver["search_space_size"]
    assert solver["best_known_objective"] == pytest.approx(summary["objective_value"])
    assert "uniform_baseline_objective" in solver

    grid = pd.read_csv(tmp_path / "grid_results.csv")
    assert list(grid.columns) == GRID_RESULT_COLUMNS
    assert len(grid) == 4
    tier_rates = json.loads((tmp_path / "segmented_tier_rates.json").read_text(encoding="utf-8"))
    assert tier_rates == summary["segmented_tier_rates"]
    assert set(tier_rates["loan_tiers"]) == {"standard", "high_lvr"}
    assert set(tier_rates["deposit_tiers"]) == {"small", "large"}


def test_joint_artifacts_do_not_gain_segmented_solver_key(tmp_path):
    config = load_config("configs/base.yaml").model_copy(
        update={"simulation": {"n_loan_customers": 200, "n_deposit_customers": 200}}
    )
    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=0)

    diagnostics.write_artifacts(tmp_path)

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert "segmented_solver" not in summary


def test_runner_reports_coordinate_descent_when_search_space_is_large(monkeypatch):
    import balance_pricing_engine.scenarios.runner as runner

    monkeypatch.setattr(
        runner, "SEGMENTED_SOLVER_SETTINGS", SolverSettings(exact_search_max_combinations=10)
    )
    diagnostics = run_baseline_diagnostics(
        EngineConfig.model_validate(_two_tier_raw_config()), max_boundary_expansions=0
    )

    assert diagnostics.segmented_solver["solver_method"] == "COORDINATE_DESCENT"
    assert diagnostics.segmented_solver["optimality_proven"] is False
    assert isinstance(diagnostics.segmented_solver["solver_converged"], bool)


# ----------------------------------------------------------------- settings guards


def test_solver_settings_reject_unbounded_resources():
    with pytest.raises(ValueError):
        SolverSettings(max_iterations=0)
    with pytest.raises(ValueError):
        SolverSettings(max_starts=0)
    with pytest.raises(ValueError):
        SolverSettings(exact_search_max_combinations=10**12)
    with pytest.raises(ValueError):
        SolverSettings(method="random")


def test_forced_exact_search_beyond_hard_cap_is_rejected():
    big = [_loan(f"l{i}", np.linspace(0.03, 0.09, 200), np.ones(200)) for i in range(4)]
    with pytest.raises(ValueError, match="exact"):
        solve_segmented_portfolio(big, [_deposit("d", [0.03], [1.0])], _params(), settings=EXACT)


def test_tier_result_choice_round_trips_candidate_values():
    tier = _loan("a", [0.05, 0.06], [100.0, 50.0])

    choice = tier.choice(1)

    assert isinstance(choice, TierResult)
    assert (choice.rate, choice.volume, choice.interest_income) == (0.06, 50.0, 3.0)


# ------------------------------------------------------------------------- helpers


def _optimizer_kwargs(config: EngineConfig) -> dict:
    return {
        "market": MarketState(**config.market.model_dump()),
        "config": config.segmented_pricing,
        "loan_rate_elasticity": config.response.loan_rate_elasticity,
        "loan_competitor_gap_elasticity": config.response.loan_competitor_gap_elasticity,
        "deposit_rate_elasticity": config.response.deposit_rate_elasticity,
        "deposit_competitor_gap_elasticity": config.response.deposit_competitor_gap_elasticity,
        "required_deposit_funding_ratio": config.balance_sheet.required_deposit_funding_ratio,
        "funding_shortfall_penalty_rate": config.balance_sheet.funding_shortfall_penalty_rate,
        "loan_rwa_weight": config.balance_sheet.loan_rwa_weight,
        "capital_hurdle_rate": config.balance_sheet.capital_hurdle_rate,
        "response_coefficients": config.response.coefficients,
    }


def _two_tier_raw_config() -> dict:
    raw = _base_raw_config()
    raw["segmented_pricing"] = {
        "loan_tiers": [
            {
                "name": "standard",
                "lvr_max": 0.80,
                "rate_grid": {"start": 0.040, "stop": 0.070, "step": 0.003},
            },
            {"name": "high_lvr", "rate_grid": {"start": 0.040, "stop": 0.070, "step": 0.003}},
        ],
        "deposit_tiers": [
            {
                "name": "small",
                "balance_max": 100_000,
                "rate_grid": {"start": 0.030, "stop": 0.048, "step": 0.003},
            },
            {"name": "large", "rate_grid": {"start": 0.030, "stop": 0.048, "step": 0.003}},
        ],
    }
    return raw


def _gates(config, diagnostics):
    from balance_pricing_engine.governance.lineage import synthetic_model_lineage

    return evaluate_gates(
        config=config,
        diagnostics=diagnostics,
        model_lineage={
            "loan": synthetic_model_lineage("loan"),
            "deposit": synthetic_model_lineage("deposit"),
        },
        feature_lineage={},
        competitor_rate_issues=None,
    )


def test_tier_candidates_reject_malformed_arrays():
    with pytest.raises(ValueError, match="differ in length"):
        LoanTierCandidates("a", [0.05, 0.06], [1.0], [0.05], [0.0])
    with pytest.raises(ValueError, match="differ in length"):
        DepositTierCandidates("d", [0.03], [1.0, 2.0], [0.03])
    with pytest.raises(ValueError, match="non-empty"):
        _deposit("d", [], [])
    with pytest.raises(ValueError, match="finite"):
        _loan("a", [0.05, float("nan")], [1.0, 1.0])


def test_coordinate_descent_rejects_invalid_starts():
    loans, deposits = [_loan("a", [0.05, 0.06], [1.0, 1.0])], [_deposit("d", [0.03], [1.0])]
    with pytest.raises(ValueError, match="every tier"):
        run_coordinate_descent(loans, deposits, _params(), start=((0, 0), (0,)))
    with pytest.raises(ValueError, match="outside"):
        run_coordinate_descent(loans, deposits, _params(), start=((2,), (0,)))


def test_portfolio_evaluation_requires_both_sides():
    with pytest.raises(ValueError, match="At least one"):
        evaluate_segmented_portfolio([], [_deposit("d", [0.03], [1.0]).choice(0)], _params())
    with pytest.raises(ValueError, match="At least one"):
        solve_segmented_portfolio([_loan("a", [0.05], [1.0])], [], _params())
