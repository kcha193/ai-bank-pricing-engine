"""TEST-002: portable regression anchors for OPT-001, OPT-002 and FIN-002.

These three properties use only public APIs that already existed at the pre-fix revisions
(audit ``62b81d2``, pre-3a ``3304b20``, pre-3b ``5141833``), so this exact file can be run
unmodified against those checkouts (``docs/audits/2026-11-21-phase-3c-evidence``). Each
fails there and passes on current master. The richer, non-portable versions live in
``test_invariants_governance.py`` (G02-G05), ``test_invariants_optimisation.py`` (D01) and
``test_invariants_multiperiod.py`` (M12-M16).

Imports are limited to: ``config.EngineConfig`` and the segmented tier configs,
``scenarios.runner.run_baseline_diagnostics``, ``scenarios.multiperiod``,
``optimization.segmented.optimize_segmented_rates`` (positional signature) and
``optimization.grid_search`` (``build_rate_grid``, ``evaluate_joint_rate_grid``,
``select_best_result``).
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from economic_strategies import (
    FundingScenario,
    budget,
    funding_scenarios,
    market_rate_grids,
    money_close,
)
from hypothesis import example, given
from hypothesis import strategies as st

from balance_pricing_engine.config import (
    DepositTierConfig,
    EngineConfig,
    GridConfig,
    LoanTierConfig,
    SegmentedPricingConfig,
)
from balance_pricing_engine.optimization.grid_search import (
    build_rate_grid,
    evaluate_joint_rate_grid,
    select_best_result,
)
from balance_pricing_engine.optimization.segmented import optimize_segmented_rates
from balance_pricing_engine.scenarios.multiperiod import run_multiperiod_diagnostics
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.synthetic.market import MarketState

pytestmark = [
    pytest.mark.economic_invariants,
    pytest.mark.filterwarnings("ignore::UserWarning"),
]

_RESPONSE = {
    "loan_rate_elasticity": -170.0,
    "loan_competitor_gap_elasticity": -130.0,
    "deposit_rate_elasticity": 140.0,
    "deposit_competitor_gap_elasticity": 110.0,
}


def _raw_config(scenario, loan_grid=(0.045, 0.065, 0.005), deposit_grid=(0.03, 0.045, 0.005),
                seed=7, n=120):
    return {
        "random_seed": seed,
        "simulation": {"n_loan_customers": n, "n_deposit_customers": n},
        "market": {
            "cash_rate": scenario.cash_rate,
            "wholesale_funding_spread": scenario.wholesale_funding_rate - scenario.cash_rate,
            "competitor_loan_rate": 0.0499,
            "competitor_deposit_rate": 0.039,
            "home_loan_reference_rate": 0.0499,
            "term_deposit_reference_rate": 0.039,
        },
        "products": {
            "loan_rate_grid": dict(zip(("start", "stop", "step"), loan_grid)),
            "deposit_rate_grid": dict(zip(("start", "stop", "step"), deposit_grid)),
        },
        "balance_sheet": {
            "required_deposit_funding_ratio": scenario.required_deposit_funding_ratio,
            "funding_shortfall_penalty_rate": scenario.funding_shortfall_penalty_rate,
            "loan_rwa_weight": scenario.loan_rwa_weight,
            "capital_hurdle_rate": scenario.capital_hurdle_rate,
        },
        "response": dict(_RESPONSE),
    }


# ----------------------------------------------------------------------- OPT-001


@given(
    scenario=funding_scenarios(),
    seed=st.integers(0, 1_000),
    kind=st.sampled_from(["reachable", "unreachable"]),
    quantile=st.floats(0.0, 1.0),
)
@example(
    scenario=FundingScenario(
        required_deposit_funding_ratio=0.65,
        funding_shortfall_penalty_rate=0.018,
        wholesale_funding_rate=0.04,
        cash_rate=0.03,
        loan_rwa_weight=0.35,
        capital_hurdle_rate=0.12,
    ),
    seed=7,
    kind="unreachable",
    quantile=1.0,
)
@budget(20)
def test_a1_opt001_roe_floor_is_met_or_its_relaxation_is_disclosed(
    scenario, seed, kind, quantile
):
    """With a configured floor: if any finite candidate on the final grid reaches it, the
    winner reaches it; otherwise the result must say the floor was relaxed. The audited code
    silently returned the unconstrained winner with no disclosure."""
    raw = _raw_config(scenario, seed=seed)
    probe = run_baseline_diagnostics(EngineConfig.model_validate(raw), max_boundary_expansions=1)
    roe = np.sort(probe.grid_results["return_on_equity"].to_numpy())
    if kind == "reachable":
        floor = max(0.0, float(roe[int(quantile * (len(roe) - 1))]))
    else:
        floor = max(0.0, float(roe[-1])) + 1.0
    raw["balance_sheet"]["min_return_on_equity"] = floor
    diagnostics = run_baseline_diagnostics(
        EngineConfig.model_validate(raw), max_boundary_expansions=1
    )
    final_roe = diagnostics.grid_results["return_on_equity"].to_numpy()
    reachable = bool(np.any(np.isfinite(final_roe) & (final_roe >= floor)))
    disclosed = getattr(diagnostics, "roe_constraint_relaxed", None)
    best = diagnostics.best_result
    winner = diagnostics.grid_results[
        np.isclose(diagnostics.grid_results["loan_rate"], best.loan_rate)
        & np.isclose(diagnostics.grid_results["deposit_rate"], best.deposit_rate)
    ]
    winner_roe = float(winner["return_on_equity"].iloc[0])
    if reachable:
        assert winner_roe >= floor, (winner_roe, floor)
        assert disclosed is False, f"reachable floor {floor}: relaxation flag {disclosed!r}"
    else:
        assert disclosed is True, (
            f"OPT-001 undisclosed relaxation: floor {floor} (max ROE {np.nanmax(final_roe)}), flag "
            f"{disclosed!r}, winner ROE {winner_roe}"
        )


# ----------------------------------------------------------------------- OPT-002


@st.composite
def _single_tier_case(draw):
    scenario = draw(funding_scenarios())
    rng = np.random.default_rng(draw(st.integers(0, 10_000)))
    loans = generate_loan_customers(draw(st.integers(20, 80)), rng)
    deposits = generate_deposit_customers(draw(st.integers(20, 80)), rng)
    return (
        scenario,
        loans,
        deposits,
        draw(market_rate_grids(0.0499)),
        draw(market_rate_grids(0.039)),
    )


@given(case=_single_tier_case())
@budget(60)
def test_a2_opt002_single_tier_segmented_equals_joint_grid_search(case):
    """One catch-all tier per side is the joint problem, so both optimisers must pick the
    same rates and objective. The pre-3a tier-wise search ignored the shortfall penalty when
    choosing loan rates and fixed loans before deposits."""
    scenario, loans, deposits, loan_spec, deposit_spec = case
    market = MarketState(
        cash_rate=scenario.cash_rate,
        wholesale_funding_spread=scenario.wholesale_funding_rate - scenario.cash_rate,
        competitor_loan_rate=0.0499,
        competitor_deposit_rate=0.039,
        home_loan_reference_rate=0.0499,
        term_deposit_reference_rate=0.039,
    )
    tiers = SegmentedPricingConfig(
        loan_tiers=[LoanTierConfig(name="all", lvr_max=None, rate_grid=GridConfig(
            start=loan_spec[0], stop=loan_spec[1], step=loan_spec[2]))],
        deposit_tiers=[DepositTierConfig(name="all", balance_max=None, rate_grid=GridConfig(
            start=deposit_spec[0], stop=deposit_spec[1], step=deposit_spec[2]))],
    )
    funding = (scenario.required_deposit_funding_ratio, scenario.funding_shortfall_penalty_rate)
    segmented = optimize_segmented_rates(
        loans, deposits, market, tiers,
        _RESPONSE["loan_rate_elasticity"], _RESPONSE["loan_competitor_gap_elasticity"],
        _RESPONSE["deposit_rate_elasticity"], _RESPONSE["deposit_competitor_gap_elasticity"],
        *funding, scenario.loan_rwa_weight, scenario.capital_hurdle_rate,
    )
    joint = select_best_result(
        evaluate_joint_rate_grid(
            loans, deposits, market, build_rate_grid(*loan_spec), build_rate_grid(*deposit_spec),
            *_RESPONSE.values(), *funding,
            rwa_weight=scenario.loan_rwa_weight,
            capital_hurdle_rate=scenario.capital_hurdle_rate,
        )
    )
    evidence = {
        "segmented": (segmented.loan_tiers[0].rate, segmented.deposit_tiers[0].rate,
                      segmented.total_objective),
        "joint": (joint.loan_rate, joint.deposit_rate, joint.total_objective),
    }
    message = f"OPT-002 segmented/joint mismatch: {evidence}"
    assert money_close(segmented.total_objective, joint.total_objective), message
    assert math.isclose(segmented.loan_tiers[0].rate, joint.loan_rate, abs_tol=1e-12), message
    assert math.isclose(segmented.deposit_tiers[0].rate, joint.deposit_rate,
                        abs_tol=1e-12), message
    assert money_close(segmented.funding_shortfall_penalty,
                       joint.funding_shortfall_penalty), message


# ----------------------------------------------------------------------- FIN-002


@given(scenario=funding_scenarios())
@budget(10)
def test_a3_fin002_unrun_back_book_doubles_the_period_zero_contribution(scenario):
    """Zero runoff, drift and discount: period 1 holds the period-0 book at its booked terms
    plus an identical new cohort, so its contribution is exactly twice period 0 (every term
    is positively homogeneous). The pre-3b roll repriced surviving loans at the cash rate and
    dropped their ECL, capital and funding."""
    raw = _raw_config(scenario)
    raw["multiperiod"] = {
        "n_periods": 2,
        "loan_runoff_rate": 0.0,
        "deposit_runoff_rate": 0.0,
        "discount_rate": 0.0,
    }
    diagnostics = run_multiperiod_diagnostics(
        EngineConfig.model_validate(raw), max_boundary_expansions=0
    )
    period_zero = diagnostics.period_diagnostics[0].best_result.total_objective
    contributions = diagnostics.period_summary()["discounted_contribution"].tolist()
    evidence = {"period_zero_objective": period_zero, "contributions": contributions}
    assert money_close(contributions[0], period_zero), evidence
    assert money_close(contributions[1], 2.0 * period_zero), f"FIN-002 back-book mismatch: {evidence}"
    assert money_close(diagnostics.npv, 3.0 * period_zero), evidence
