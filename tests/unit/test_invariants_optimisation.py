"""TEST-002: joint/segmented optimisation invariants (OPT-002 regression, S01-S12).

Single-tier equivalence is generated across funding regimes, elasticities, grids and ROE
floors, rather than the handful of fixed cases in ``test_segmented_joint_optimisation.py``.
Multi-tier properties compare the solver with ``economic_strategies.brute_force_segmented``,
an independent scalar enumeration that never calls the production objective. Grids stay at
three rates or fewer per tier and three tiers or fewer per side (at most 729 combinations),
so the exhaustive reference is cheap in CI.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from economic_strategies import (
    FundingScenario,
    TierSpec,
    brute_force_segmented,
    budget,
    deposit_elasticities,
    deposit_tier_specs,
    funding_scenarios,
    loan_elasticities,
    loan_tier_specs,
    market_rate_grids,
    money_close,
    volumes,
)
from hypothesis import example, given
from hypothesis import strategies as st

from balance_pricing_engine.config import DepositTierConfig, GridConfig, LoanTierConfig
from balance_pricing_engine.config import SegmentedPricingConfig
from balance_pricing_engine.optimization.grid_search import (
    build_rate_grid,
    evaluate_joint_rate_grid,
    select_best_result_with_disclosure,
)
from balance_pricing_engine.optimization.segmented import (
    MAX_EXACT_SEARCH_COMBINATIONS,
    SOLVER_COORDINATE_DESCENT,
    SOLVER_EXACT,
    DepositTierCandidates,
    LoanTierCandidates,
    PortfolioParameters,
    SolverSettings,
    optimize_segmented_rates,
    run_coordinate_descent,
    solve_segmented_portfolio,
)
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.synthetic.market import MarketState

pytestmark = pytest.mark.economic_invariants

EXACT = SolverSettings(method="exact")
DESCENT = SolverSettings(method="coordinate_descent")

EQUIVALENCE_FIELDS = {
    # segmented attribute: joint OptimizationResult attribute
    "total_objective": "total_objective",
    "total_loan_volume": "expected_loan_volume",
    "total_deposit_volume": "expected_deposit_volume",
    "total_loan_interest_income": "loan_interest_income",
    "total_credit_loss": "expected_credit_loss",
    "capital_charge": "capital_charge",
    "total_deposit_interest_cost": "deposit_interest_cost",
    "wholesale_funding_cost": "wholesale_funding_cost",
    "funding_shortfall": "funding_shortfall",
    "funding_shortfall_penalty": "funding_shortfall_penalty",
    "excess_deposit_income": "excess_deposit_income",
}


# ------------------------------------------------- D. single-tier equivalence


@st.composite
def _single_tier_problem(draw):
    seed = draw(st.integers(0, 10_000))
    rng = np.random.default_rng(seed)
    loans = generate_loan_customers(draw(st.integers(20, 80)), rng)
    deposits = generate_deposit_customers(draw(st.integers(20, 80)), rng)
    scenario = draw(funding_scenarios())
    # Regimes the roadmap calls out explicitly, mixed with the generic draw.
    regime = draw(
        st.sampled_from(["generic", "zero_ratio", "full_ratio", "zero_penalty", "high_penalty"])
    )
    overrides = {
        "zero_ratio": {"required_deposit_funding_ratio": 0.0},
        "full_ratio": {"required_deposit_funding_ratio": 1.0},
        "zero_penalty": {"funding_shortfall_penalty_rate": 0.0},
        "high_penalty": {"funding_shortfall_penalty_rate": 0.30},
    }.get(regime, {})
    scenario = FundingScenario(**{**vars(scenario), **overrides})
    competitor_loan = draw(st.integers(300, 800)) / 10_000
    competitor_deposit = draw(st.integers(100, 600)) / 10_000
    market = MarketState(
        cash_rate=scenario.cash_rate,
        wholesale_funding_spread=scenario.wholesale_funding_rate - scenario.cash_rate,
        competitor_loan_rate=competitor_loan,
        competitor_deposit_rate=competitor_deposit,
        home_loan_reference_rate=competitor_loan,
        term_deposit_reference_rate=competitor_deposit,
    )
    if draw(st.booleans()):
        elasticities = {
            "loan_rate_elasticity": draw(loan_elasticities),
            "loan_competitor_gap_elasticity": draw(loan_elasticities),
            "deposit_rate_elasticity": draw(deposit_elasticities),
            "deposit_competitor_gap_elasticity": draw(deposit_elasticities),
        }
    else:  # within +/-50% of the calibrated base elasticities
        elasticities = {
            "loan_rate_elasticity": draw(st.floats(-255.0, -85.0)),
            "loan_competitor_gap_elasticity": draw(st.floats(-195.0, -65.0)),
            "deposit_rate_elasticity": draw(st.floats(70.0, 210.0)),
            "deposit_competitor_gap_elasticity": draw(st.floats(55.0, 165.0)),
        }
    return {
        "loans": loans,
        "deposits": deposits,
        "market": market,
        "scenario": scenario,
        "loan_grid": draw(market_rate_grids(competitor_loan)),
        "deposit_grid": draw(market_rate_grids(competitor_deposit)),
        "elasticities": elasticities,
        "floor_kind": draw(st.sampled_from(["none", "none", "feasible", "infeasible"])),
        "floor_quantile": draw(st.floats(0.0, 1.0)),
    }


def _segmented_config(loan_grid, deposit_grid) -> SegmentedPricingConfig:
    def grid(spec):
        start, stop, step = spec
        return GridConfig(start=start, stop=stop, step=step)

    return SegmentedPricingConfig(
        loan_tiers=[LoanTierConfig(name="all", lvr_max=None, rate_grid=grid(loan_grid))],
        deposit_tiers=[
            DepositTierConfig(name="all", balance_max=None, rate_grid=grid(deposit_grid))
        ],
    )


@given(problem=_single_tier_problem())
@budget(60)
def test_d01_single_tier_segmented_equals_joint_optimiser(problem):
    """OPT-002 regression: one catch-all tier per side is the joint grid search, across
    zero/high funding ratios and penalties, excess and scarce deposits, and feasible,
    equality and infeasible ROE floors. Winner and every component must agree."""
    scenario = problem["scenario"]
    market = problem["market"]
    common = dict(
        required_deposit_funding_ratio=scenario.required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=scenario.funding_shortfall_penalty_rate,
        **problem["elasticities"],
    )
    loan_grid = build_rate_grid(*problem["loan_grid"])
    deposit_grid = build_rate_grid(*problem["deposit_grid"])
    grid = evaluate_joint_rate_grid(
        problem["loans"],
        problem["deposits"],
        market,
        loan_grid,
        deposit_grid,
        rwa_weight=scenario.loan_rwa_weight,
        capital_hurdle_rate=scenario.capital_hurdle_rate,
        home_loan_reference_rate=market.home_loan_reference_rate,
        term_deposit_reference_rate=market.term_deposit_reference_rate,
        **common,
    )
    roe = np.sort(grid["return_on_equity"].to_numpy())
    floor = {
        "none": None,
        # An observed ROE value: includes the "floor exactly equals a candidate" boundary.
        "feasible": float(roe[int(problem["floor_quantile"] * (len(roe) - 1))]),
        "infeasible": float(roe[-1]) + 1.0,
    }[problem["floor_kind"]]
    if floor is not None and floor < 0:
        floor = 0.0  # config contract: min_return_on_equity >= 0
    joint = select_best_result_with_disclosure(grid, min_return_on_equity=floor)
    segmented = optimize_segmented_rates(
        problem["loans"],
        problem["deposits"],
        market,
        _segmented_config(problem["loan_grid"], problem["deposit_grid"]),
        loan_rwa_weight=scenario.loan_rwa_weight,
        capital_hurdle_rate=scenario.capital_hurdle_rate,
        home_loan_reference_rate=market.home_loan_reference_rate,
        term_deposit_reference_rate=market.term_deposit_reference_rate,
        min_return_on_equity=floor,
        **common,
    )
    best = joint.best_result
    np.testing.assert_array_equal(segmented.candidate_rates["loan_tiers"]["all"], loan_grid)
    np.testing.assert_array_equal(segmented.candidate_rates["deposit_tiers"]["all"], deposit_grid)
    assert segmented.loan_tiers[0].rate == best.loan_rate
    assert segmented.deposit_tiers[0].rate == best.deposit_rate
    for ours, theirs in EQUIVALENCE_FIELDS.items():
        assert math.isclose(
            getattr(segmented, ours), getattr(best, theirs), rel_tol=1e-12, abs_tol=1e-9
        ), ours
    assert segmented.roe_constraint_relaxed == joint.roe_constraint_relaxed
    assert segmented.return_on_equity == pytest.approx(
        joint.selected_return_on_equity, rel=1e-12, abs=1e-9
    )
    assert segmented.solver.optimality_proven
    assert segmented.solver.solver_method == SOLVER_EXACT


# ------------------------------------------------------ E. multi-tier properties


def _loan_candidates(specs):
    return [
        LoanTierCandidates(f"loan{i}", s.rates, s.volume, s.interest, s.credit_loss)
        for i, s in enumerate(specs)
    ]


def _deposit_candidates(specs):
    return [
        DepositTierCandidates(f"deposit{i}", s.rates, s.volume, s.interest)
        for i, s in enumerate(specs)
    ]


def _parameters(scenario: FundingScenario) -> PortfolioParameters:
    return PortfolioParameters(
        required_deposit_funding_ratio=scenario.required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=scenario.funding_shortfall_penalty_rate,
        loan_rwa_weight=scenario.loan_rwa_weight,
        capital_hurdle_rate=scenario.capital_hurdle_rate,
        wholesale_funding_rate=scenario.wholesale_funding_rate,
        cash_rate=scenario.cash_rate,
    )


@st.composite
def _multi_tier_problem(draw, max_tiers: int = 3):
    loan_specs = draw(st.lists(loan_tier_specs(), min_size=1, max_size=max_tiers))
    deposit_specs = draw(st.lists(deposit_tier_specs(), min_size=1, max_size=max_tiers))
    scenario = draw(funding_scenarios())
    if draw(st.booleans()):
        # Scarce-funding regime: a binding deposit-funding requirement with a material
        # penalty, where loan and deposit tier choices interact (the OPT-002 defect class).
        scenario = FundingScenario(
            **{
                **vars(scenario),
                "required_deposit_funding_ratio": draw(st.floats(0.5, 1.0)),
                "funding_shortfall_penalty_rate": draw(st.floats(0.05, 0.30)),
            }
        )
    return loan_specs, deposit_specs, scenario


def _draw_floor(draw, ranked):
    """None, an observed candidate ROE (feasible, possibly equal), or above every ROE."""
    kind = draw(st.sampled_from(["none", "observed", "infeasible"]))
    roes = sorted(w.terms.return_on_equity for w in ranked)
    if kind == "none":
        return None
    if kind == "infeasible":
        return max(roes[-1], 0.0) + 1.0
    return max(0.0, roes[draw(st.integers(0, len(roes) - 1))])


def _assert_matches_reference(search, ranked, floor):
    best = ranked[0]
    result = search.result
    chosen = next(
        w for w in ranked
        if w.loan_indices == search.loan_indices and w.deposit_indices == search.deposit_indices
    )
    # S01: the solver's winner is a brute-force optimum (ties within tolerance allowed).
    assert chosen.meets_floor == best.meets_floor
    assert money_close(chosen.terms.total_objective, best.terms.total_objective)
    assert money_close(chosen.terms.funding_shortfall, best.terms.funding_shortfall)
    assert money_close(result.total_objective, chosen.terms.total_objective)
    # S05: constraint-first; relaxation disclosed exactly when nothing qualifies.
    any_qualifies = floor is not None and any(w.meets_floor for w in ranked)
    if floor is None:
        assert search.roe_constraint_relaxed is False
    else:
        assert search.roe_constraint_relaxed is (not any_qualifies)
        if any_qualifies:
            assert result.return_on_equity >= floor


@st.composite
def _exact_search_case(draw):
    loan_specs, deposit_specs, scenario = draw(_multi_tier_problem())
    floor = _draw_floor(draw, brute_force_segmented(loan_specs, deposit_specs, scenario))
    return loan_specs, deposit_specs, scenario, floor


@given(case=_exact_search_case())
# The higher rate gives up $0.20 of interest, but saves $0.16 in each cost.
# Dropping either the funding penalty or capital charge reverses the winner.
@example(case=(
    [
        TierSpec(rates=(0.05, 0.06), volume=(100.0, 80.0), interest=(5.0, 4.8),
                 credit_loss=(0.0, 0.0)),
        TierSpec(rates=(0.05,), volume=(10.0,), interest=(0.5,), credit_loss=(0.0,)),
    ],
    [TierSpec(rates=(0.01,), volume=(10.0,), interest=(0.1,))],
    FundingScenario(
        required_deposit_funding_ratio=1.0,
        funding_shortfall_penalty_rate=0.008,
        wholesale_funding_rate=0.0,
        cash_rate=0.0,
        loan_rwa_weight=1.0,
        capital_hurdle_rate=0.08,
    ),
    None,
))
# Three loan tiers distinguish an ordered sum from Python 3.12's compensated sum.
@example(case=(
    [
        TierSpec(rates=(0.03,), volume=(512928530.0,), interest=(15387855.899999999,),
                 credit_loss=(0.0,)),
        TierSpec(rates=(0.03, 0.0305), volume=(34601131.0, 17300565.5),
                 interest=(1038033.9299999999, 527667.24775), credit_loss=(0.0, 0.0)),
        TierSpec(rates=(0.048,), volume=(256464262.0,), interest=(12310284.576,),
                 credit_loss=(0.0,)),
    ],
    [TierSpec(rates=(0.01,), volume=(10_000_000.0,), interest=(100_000.0,))],
    FundingScenario(
        required_deposit_funding_ratio=0.0,
        funding_shortfall_penalty_rate=0.0,
        wholesale_funding_rate=0.0,
        cash_rate=0.0,
        loan_rwa_weight=1.0,
        capital_hurdle_rate=0.0,
    ),
    0.3561740155839462,
))
@budget(100)
def test_s01_s02_s03_s05_exact_search_matches_independent_brute_force(case):
    loan_specs, deposit_specs, scenario, floor = case
    ranked = brute_force_segmented(loan_specs, deposit_specs, scenario, floor)
    search = solve_segmented_portfolio(
        _loan_candidates(loan_specs),
        _deposit_candidates(deposit_specs),
        _parameters(scenario),
        settings=EXACT,
        min_return_on_equity=floor,
    )
    _assert_matches_reference(search, ranked, floor)
    # S03: every selected rate is on its own tier grid.
    for tier, spec in zip(search.result.loan_tiers, loan_specs):
        assert tier.rate in spec.rates
    for tier, spec in zip(search.result.deposit_tiers, deposit_specs):
        assert tier.rate in spec.rates
    # S10: exhaustive search is the only method that claims proven optimality.
    assert search.diagnostics.solver_method == SOLVER_EXACT
    assert search.diagnostics.optimality_proven is True


@st.composite
def _shared_grid_problem(draw):
    """Tiers on each side share one grid, so a uniform-price comparator exists."""
    n_loan = draw(st.integers(1, 3))
    n_deposit = draw(st.integers(1, 3))
    loan_rates = tuple(sorted(set(draw(st.lists(st.integers(300, 900), min_size=1, max_size=3)))))
    deposit_rates = tuple(
        sorted(set(draw(st.lists(st.integers(100, 600), min_size=1, max_size=3))))
    )

    def spec(bp_rates, loan):
        tier_rates = tuple(r / 10_000 for r in bp_rates)
        tier_volumes = tuple(draw(st.lists(volumes, min_size=len(tier_rates),
                                           max_size=len(tier_rates))))
        loss = draw(st.floats(0.0, 0.03)) if loan else 0.0
        return TierSpec(
            rates=tier_rates,
            volume=tier_volumes,
            interest=tuple(r * v for r, v in zip(tier_rates, tier_volumes)),
            credit_loss=tuple(v * loss for v in tier_volumes) if loan else (),
        )

    return (
        [spec(loan_rates, True) for _ in range(n_loan)],
        [spec(deposit_rates, False) for _ in range(n_deposit)],
        draw(funding_scenarios()),
    )


@given(problem=_shared_grid_problem(), method=st.sampled_from(["exact", "coordinate_descent"]))
@budget(80)
def test_s04_segmented_is_never_worse_than_a_feasible_uniform_price(problem, method):
    loan_specs, deposit_specs, scenario = problem
    search = solve_segmented_portfolio(
        _loan_candidates(loan_specs),
        _deposit_candidates(deposit_specs),
        _parameters(scenario),
        settings=SolverSettings(method=method),
    )
    diagnostics = search.diagnostics
    assert diagnostics.uniform_baseline_feasible
    assert search.result.total_objective >= diagnostics.uniform_baseline_objective - 1e-6
    # Independent uniform comparator: best objective charging one rate per side.
    uniform_best = max(
        w.terms.total_objective
        for w in brute_force_segmented(loan_specs, deposit_specs, scenario)
        if len(set(w.loan_indices)) == 1 and len(set(w.deposit_indices)) == 1
    )
    assert search.result.total_objective >= uniform_best - max(1e-6, 1e-9 * abs(uniform_best))
    # S10: coordinate descent never claims proven optimality.
    if method == "coordinate_descent":
        assert diagnostics.solver_method == SOLVER_COORDINATE_DESCENT
        assert diagnostics.optimality_proven is False


@given(problem=_multi_tier_problem(), data=st.data())
@budget(80)
def test_s06_s11_coordinate_descent_moves_never_worsen_the_ranking_key(problem, data):
    """S06: accepted moves never lower the (meets floor, objective) key. S11: a run that did
    not converge discloses the iteration cap (or a cycle); a converged run does not."""
    loan_specs, deposit_specs, scenario = problem
    ranked = brute_force_segmented(loan_specs, deposit_specs, scenario)
    floor = _draw_floor(data.draw, ranked)
    start = (
        tuple(data.draw(st.integers(0, len(s.rates) - 1)) for s in loan_specs),
        tuple(data.draw(st.integers(0, len(s.rates) - 1)) for s in deposit_specs),
    )
    max_iterations = data.draw(st.integers(1, 4))
    run = run_coordinate_descent(
        _loan_candidates(loan_specs),
        _deposit_candidates(deposit_specs),
        _parameters(scenario),
        start=start,
        settings=SolverSettings(method="coordinate_descent", max_iterations=max_iterations),
        min_return_on_equity=floor,
    )
    initial = brute_force_segmented(loan_specs, deposit_specs, scenario, floor)
    by_choice = {(w.loan_indices, w.deposit_indices): w for w in initial}
    first, last = by_choice[start], by_choice[(run.loan_indices, run.deposit_indices)]
    assert (last.meets_floor, last.terms.total_objective) >= (
        first.meets_floor,
        first.terms.total_objective - max(1e-6, 1e-9 * abs(first.terms.total_objective)),
    )
    if floor is None:
        steps = np.diff(run.accepted_objectives)
        assert np.all(steps > 0)
    assert run.iterations <= max_iterations
    assert not (run.converged and run.iteration_limit_reached)
    if not run.converged:
        assert run.iteration_limit_reached or run.cycle_detected


@given(problem=_multi_tier_problem(), method=st.sampled_from(["exact", "coordinate_descent"]))
@budget(40)
def test_s07_solver_is_deterministic(problem, method):
    loan_specs, deposit_specs, scenario = problem
    settings = SolverSettings(method=method)
    first, second = (
        solve_segmented_portfolio(
            _loan_candidates(loan_specs),
            _deposit_candidates(deposit_specs),
            _parameters(scenario),
            settings=settings,
        )
        for _ in range(2)
    )
    assert (first.loan_indices, first.deposit_indices) == (
        second.loan_indices,
        second.deposit_indices,
    )
    assert first.result.total_objective == second.result.total_objective
    assert first.diagnostics.to_dict() == second.diagnostics.to_dict()


@given(problem=_multi_tier_problem(), data=st.data())
@budget(80)
def test_s09_enlarging_an_exact_search_space_never_lowers_the_best_key(problem, data):
    """Adding a candidate rate to one tier keeps every old combination feasible, so the
    exact optimum's (meets floor, objective) key cannot fall."""
    loan_specs, deposit_specs, scenario = problem
    floor = _draw_floor(data.draw, brute_force_segmented(loan_specs, deposit_specs, scenario))
    side = data.draw(st.sampled_from(["loan", "deposit"]))
    specs = loan_specs if side == "loan" else deposit_specs
    index = data.draw(st.integers(0, len(specs) - 1))
    tier = specs[index]
    new_rate = round(max(tier.rates) + 0.0010, 6)
    new_volume = data.draw(volumes)
    enlarged = TierSpec(
        rates=(*tier.rates, new_rate),
        volume=(*tier.volume, new_volume),
        interest=(*tier.interest, new_rate * new_volume),
        credit_loss=(*tier.credit_loss, new_volume * 0.01) if side == "loan" else (),
    )
    bigger = list(specs)
    bigger[index] = enlarged
    loan_big = bigger if side == "loan" else loan_specs
    deposit_big = bigger if side == "deposit" else deposit_specs

    def key(loans, deposits):
        search = solve_segmented_portfolio(
            _loan_candidates(loans),
            _deposit_candidates(deposits),
            _parameters(scenario),
            settings=EXACT,
            min_return_on_equity=floor,
        )
        return (not search.roe_constraint_relaxed, search.result.total_objective)

    small_key, big_key = key(loan_specs, deposit_specs), key(loan_big, deposit_big)
    assert big_key[0] >= small_key[0]
    if big_key[0] == small_key[0]:
        assert big_key[1] >= small_key[1] - max(1e-6, 1e-9 * abs(small_key[1]))


@given(problem=_multi_tier_problem())
@budget(40)
def test_s12_raising_the_exact_search_threshold_does_not_move_a_proven_optimum(problem):
    loan_specs, deposit_specs, scenario = problem
    space = int(np.prod([len(s.rates) for s in (*loan_specs, *deposit_specs)]))
    results = [
        solve_segmented_portfolio(
            _loan_candidates(loan_specs),
            _deposit_candidates(deposit_specs),
            _parameters(scenario),
            settings=SolverSettings(method="auto", exact_search_max_combinations=threshold),
        )
        for threshold in (space, MAX_EXACT_SEARCH_COMBINATIONS)
    ]
    assert all(r.diagnostics.optimality_proven for r in results)
    assert (results[0].loan_indices, results[0].deposit_indices) == (
        results[1].loan_indices,
        results[1].deposit_indices,
    )


def test_s11_iteration_cap_is_disclosed_not_reported_as_convergence():
    """Targeted: a start two moves from the optimum with a one-iteration cap."""
    loan = LoanTierCandidates(
        "loan", np.array([0.05, 0.06, 0.07]), np.array([3e8, 2e8, 1e8]),
        np.array([0.05 * 3e8, 0.06 * 2e8, 0.07 * 1e8]), np.zeros(3),
    )
    deposits = [
        DepositTierCandidates(name, np.array([0.02, 0.03, 0.04]), np.array(v),
                              np.array([0.02, 0.03, 0.04]) * np.array(v))
        for name, v in (("d0", [1e7, 5e7, 9e7]), ("d1", [1e7, 6e7, 9.5e7]))
    ]
    params = PortfolioParameters(0.9, 0.5, 0.35, 0.12, 0.06, 0.03)
    run = run_coordinate_descent(
        [loan], deposits, params, start=((2,), (0, 0)),
        settings=SolverSettings(method="coordinate_descent", max_iterations=1),
    )
    full = run_coordinate_descent(
        [loan], deposits, params, start=((2,), (0, 0)),
        settings=SolverSettings(method="coordinate_descent", max_iterations=50),
    )
    assert full.converged and not full.iteration_limit_reached
    assert full.iterations > 1
    assert not run.converged
    assert run.iteration_limit_reached
