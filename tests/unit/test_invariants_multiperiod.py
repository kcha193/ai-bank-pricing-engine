"""TEST-002: held-book (multi-period cohort) invariants M01-M20 (FIN-002 regression).

Cohort-level properties run on generated ``BookedCohort`` portfolios and compare with hand
calculations (survival ``(1 - runoff) ** age``, funding on aggregate balances). Engine-level
properties run the real roll on small synthetic populations across generated funding and
runoff regimes. ``test_multiperiod.py`` and ``test_cohort_accounting.py`` hold the worked
examples (T01-T44); this module generalises them over valid input domains.

Not asserted: that the back-book incremental contribution is positive. Funding interactions
and market moves can legitimately make it negative.
"""

from __future__ import annotations

import math
import warnings

import numpy as np
import pytest
from economic_strategies import (
    FundingScenario,
    budget,
    discount_rates,
    funding_scenarios,
    money_close,
    rates,
    reference_portfolio_terms,
    runoff_rates,
    volumes,
)
from hypothesis import given
from hypothesis import strategies as st

from balance_pricing_engine.config import BalanceSheetConfig, EngineConfig, MultiPeriodConfig
from balance_pricing_engine.scenarios.cohort_accounting import (
    BookedCohort,
    evaluate_active_portfolio,
    position_at,
    survival_factor,
)
from balance_pricing_engine.scenarios.multiperiod import run_multiperiod_diagnostics
from balance_pricing_engine.synthetic.market import MarketState

pytestmark = [
    pytest.mark.economic_invariants,
    # Small synthetic populations and generated funding regimes trip config sanity warnings.
    pytest.mark.filterwarnings("ignore::UserWarning:balance_pricing_engine.config"),
]

_COHORT_TERMS = ("loan_interest_income", "credit_loss", "capital_charge", "deposit_interest_cost")


@st.composite
def booked_cohorts(draw, max_origination: int = 4) -> BookedCohort:
    loan_volume = draw(volumes)
    return BookedCohort(
        origination_period=draw(st.integers(0, max_origination)),
        loan_rate=draw(rates),
        deposit_rate=draw(rates),
        original_loan_volume=loan_volume,
        original_deposit_volume=draw(volumes),
        booked_credit_loss=loan_volume * draw(st.floats(0.0, 0.03)),
        loan_rwa_weight=draw(st.floats(0.0, 1.0)),
        capital_hurdle_rate=draw(st.floats(0.0, 0.25)),
    )


def _market(scenario: FundingScenario) -> MarketState:
    return MarketState(
        cash_rate=scenario.cash_rate,
        wholesale_funding_spread=scenario.wholesale_funding_rate - scenario.cash_rate,
        competitor_loan_rate=0.05,
        competitor_deposit_rate=0.04,
    )


def _balance_sheet(scenario: FundingScenario) -> BalanceSheetConfig:
    return BalanceSheetConfig(
        required_deposit_funding_ratio=scenario.required_deposit_funding_ratio,
        funding_shortfall_penalty_rate=scenario.funding_shortfall_penalty_rate,
    )


# ------------------------------------------------------- M01-M05 survival


@given(cohort=booked_cohorts(), loan_runoff=runoff_rates, deposit_runoff=runoff_rates,
       age=st.integers(0, 12))
@budget(200)
def test_m01_to_m05_m18_survival_is_geometric_bounded_and_independent(
    cohort, loan_runoff, deposit_runoff, age
):
    period = cohort.origination_period + age
    position = position_at(cohort, period, loan_runoff, deposit_runoff)
    loan_survival = (1.0 - loan_runoff) ** age
    deposit_survival = (1.0 - deposit_runoff) ** age
    # M04 geometric (M01 at age 0; M02 zero runoff; M03 full runoff), M18 independent sides.
    assert position.loan_survival == pytest.approx(loan_survival, rel=1e-12, abs=0.0)
    assert position.deposit_survival == pytest.approx(deposit_survival, rel=1e-12, abs=0.0)
    if age == 0:
        assert position.loan_volume == cohort.original_loan_volume
        assert position.deposit_volume == cohort.original_deposit_volume
    if loan_runoff == 0.0:
        assert position.loan_volume == cohort.original_loan_volume
    if loan_runoff == 1.0 and age > 0:
        assert position.loan_volume == 0.0
    if deposit_runoff == 1.0 and age > 0:
        assert position.deposit_volume == 0.0
    # M05: surviving balances stay within [0, original].
    assert 0.0 <= position.loan_volume <= cohort.original_loan_volume
    assert 0.0 <= position.deposit_volume <= cohort.original_deposit_volume
    # M18: the deposit runoff never touches loan survival, and vice versa.
    other = position_at(cohort, period, loan_runoff, 1.0 - deposit_runoff)
    assert other.loan_volume == position.loan_volume


@given(runoff=st.floats(0.0, 1.0), age=st.integers(0, 30))
@budget(100)
def test_m04_survival_ratio_between_consecutive_periods_is_one_minus_runoff(runoff, age):
    now, later = survival_factor(runoff, age), survival_factor(runoff, age + 1)
    assert later <= now
    assert later == pytest.approx(now * (1.0 - runoff), rel=1e-12, abs=1e-300)


# --------------------------------------------- M06-M09 booked contractual terms


@given(cohort=booked_cohorts(), loan_runoff=runoff_rates, deposit_runoff=runoff_rates,
       age=st.integers(0, 6))
@budget(200)
def test_m06_to_m09_surviving_cohort_carries_booked_terms_pro_rata(
    cohort, loan_runoff, deposit_runoff, age
):
    """Interest at the booked rates, ECL at the booked loss rate, capital on surviving RWA."""
    position = position_at(cohort, cohort.origination_period + age, loan_runoff, deposit_runoff)
    loan_volume = cohort.original_loan_volume * (1.0 - loan_runoff) ** age
    deposit_volume = cohort.original_deposit_volume * (1.0 - deposit_runoff) ** age
    assert money_close(position.loan_interest_income, loan_volume * cohort.loan_rate)  # M06
    assert money_close(position.deposit_interest_cost, deposit_volume * cohort.deposit_rate)
    assert money_close(  # M08
        position.credit_loss, cohort.booked_credit_loss * (1.0 - loan_runoff) ** age
    )
    assert money_close(  # M09: RWA x tier-1 ratio x hurdle on surviving loans
        position.capital_charge,
        loan_volume * cohort.loan_rwa_weight * 0.10 * cohort.capital_hurdle_rate,
    )


@given(cohorts=st.lists(booked_cohorts(), min_size=1, max_size=4),
       scenario_a=funding_scenarios(), scenario_b=funding_scenarios(),
       loan_runoff=runoff_rates, deposit_runoff=runoff_rates)
@budget(150)
def test_m06_m07_m20_market_moves_change_only_funding_terms(
    cohorts, scenario_a, scenario_b, loan_runoff, deposit_runoff
):
    """A different market (cash, wholesale, penalty) never reprices booked contracts."""
    period = max(c.origination_period for c in cohorts)
    positions = [position_at(c, period, loan_runoff, deposit_runoff) for c in cohorts]
    a = evaluate_active_portfolio(positions, _market(scenario_a), _balance_sheet(scenario_a))
    b = evaluate_active_portfolio(positions, _market(scenario_b), _balance_sheet(scenario_b))
    for name in ("loan_interest_income", "expected_credit_loss", "capital_charge",
                 "deposit_interest_cost", "loan_volume", "deposit_volume"):
        assert getattr(a, name) == getattr(b, name), name


# ------------------------------------------ M10, M11, M17 aggregate funding


@given(cohorts=st.lists(booked_cohorts(), min_size=1, max_size=4), scenario=funding_scenarios(),
       loan_runoff=runoff_rates, deposit_runoff=runoff_rates, seed=st.integers(0, 2**32 - 1))
@budget(200)
def test_m10_m11_m17_active_book_equals_hand_objective_on_aggregate_balances(
    cohorts, scenario, loan_runoff, deposit_runoff, seed
):
    """M10: funding terms once on aggregate balances. M11: the canonical single-period
    objective (one cohort at full survival is the special case). M17: cohort order is
    irrelevant. O03: the period decomposition reconciles."""
    period = max(c.origination_period for c in cohorts)
    positions = [position_at(c, period, loan_runoff, deposit_runoff) for c in cohorts]
    economics = evaluate_active_portfolio(positions, _market(scenario), _balance_sheet(scenario))

    loan_volume = sum(p.loan_volume for p in positions)
    deposit_volume = sum(p.deposit_volume for p in positions)
    expected = reference_portfolio_terms(
        loan_volume=loan_volume,
        deposit_volume=deposit_volume,
        loan_interest_income=sum(p.loan_volume * p.cohort.loan_rate for p in positions),
        credit_loss=sum(
            p.cohort.booked_credit_loss * (1.0 - loan_runoff) ** (period - p.cohort.origination_period)
            for p in positions
        ),
        deposit_interest_cost=sum(p.deposit_volume * p.cohort.deposit_rate for p in positions),
        capital_charge=sum(
            p.loan_volume * p.cohort.loan_rwa_weight * 0.10 * p.cohort.capital_hurdle_rate
            for p in positions
        ),
        scenario=scenario,
    )
    for name in ("wholesale_funding_cost", "excess_deposit_income", "funding_shortfall",
                 "funding_shortfall_penalty", "total_objective"):
        assert money_close(getattr(economics, name), getattr(expected, name)), name
    reconciled = (
        economics.loan_interest_income - economics.expected_credit_loss - economics.capital_charge
        - economics.deposit_interest_cost - economics.wholesale_funding_cost
        - economics.funding_shortfall_penalty + economics.excess_deposit_income
    )
    assert money_close(reconciled, economics.total_objective)  # O03

    order = np.random.default_rng(seed).permutation(len(positions))
    shuffled = evaluate_active_portfolio(
        [positions[i] for i in order], _market(scenario), _balance_sheet(scenario)
    )
    for name, value in vars(economics).items():
        assert money_close(getattr(shuffled, name), value), name  # M17


@given(cohort=booked_cohorts(max_origination=0), scenario=funding_scenarios())
@budget(150)
def test_m12_identical_unrun_cohorts_double_every_term(cohort, scenario):
    """M12 at the accounting layer: zero runoff, two identical vintages => every term,
    including the nonlinear funding terms, is exactly twice the single-cohort value."""
    twin = BookedCohort(**{**vars(cohort), "origination_period": 1})
    market, sheet = _market(scenario), _balance_sheet(scenario)
    single = evaluate_active_portfolio([position_at(cohort, 1, 0.0, 0.0)], market, sheet)
    both = evaluate_active_portfolio(
        [position_at(cohort, 1, 0.0, 0.0), position_at(twin, 1, 0.0, 0.0)], market, sheet
    )
    for name, value in vars(single).items():
        assert money_close(getattr(both, name), 2.0 * value), name


# ----------------------------------------------- engine-level roll (real optimiser)


def _engine_config(scenario: FundingScenario, multiperiod: MultiPeriodConfig) -> EngineConfig:
    return EngineConfig.model_validate(
        {
            "random_seed": 7,
            "simulation": {"n_loan_customers": 120, "n_deposit_customers": 120},
            "market": {
                "cash_rate": scenario.cash_rate,
                "wholesale_funding_spread": scenario.wholesale_funding_rate - scenario.cash_rate,
                "competitor_loan_rate": 0.0499,
                "competitor_deposit_rate": 0.039,
                "home_loan_reference_rate": 0.0499,
                "term_deposit_reference_rate": 0.039,
            },
            "products": {
                "loan_rate_grid": {"start": 0.045, "stop": 0.065, "step": 0.005},
                "deposit_rate_grid": {"start": 0.030, "stop": 0.045, "step": 0.005},
            },
            "balance_sheet": {
                "required_deposit_funding_ratio": scenario.required_deposit_funding_ratio,
                "funding_shortfall_penalty_rate": scenario.funding_shortfall_penalty_rate,
                "loan_rwa_weight": scenario.loan_rwa_weight,
                "capital_hurdle_rate": scenario.capital_hurdle_rate,
            },
            "response": {
                "loan_rate_elasticity": -170.0,
                "loan_competitor_gap_elasticity": -130.0,
                "deposit_rate_elasticity": 140.0,
                "deposit_competitor_gap_elasticity": 110.0,
            },
            "multiperiod": multiperiod.model_dump(),
        }
    )


@given(scenario=funding_scenarios(), loan_runoff=runoff_rates, deposit_runoff=runoff_rates,
       discount=discount_rates, n_periods=st.integers(2, 3))
@budget(15)
def test_m13_m14_m15_m16_roll_reconciles_npv_discounting_and_attribution(
    scenario, loan_runoff, deposit_runoff, discount, n_periods
):
    """M14: NPV = sum of discounted full active-book objectives. M15: end-of-period discount
    factor (1 + r) ** -(t + 1). M16: new business + back-book increment = active book.
    M13: with full runoff of both products the back book contributes nothing."""
    config = _engine_config(
        scenario,
        MultiPeriodConfig(n_periods=n_periods, loan_runoff_rate=loan_runoff,
                          deposit_runoff_rate=deposit_runoff, discount_rate=discount),
    )
    diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)
    npv = 0.0
    for t, economics in enumerate(diagnostics.period_economics):
        factor = (1.0 + discount) ** -(t + 1)
        assert math.isclose(economics.discount_factor, factor, rel_tol=1e-12)
        assert money_close(
            economics.new_business_objective + economics.back_book_incremental_contribution,
            economics.active_portfolio_objective,
        )
        assert money_close(
            economics.new_business_objective,
            diagnostics.period_diagnostics[t].best_result.total_objective,
        )
        npv += economics.active_portfolio_objective * factor
        if t > 0 and loan_runoff == 1.0 and deposit_runoff == 1.0:
            assert money_close(economics.back_book_incremental_contribution, 0.0)
            assert economics.surviving_loan_volume == 0.0
            assert economics.surviving_deposit_volume == 0.0
    assert money_close(diagnostics.npv, npv)


@given(scenario=funding_scenarios(),
       drift_bps=st.one_of(st.integers(-50, -1), st.integers(1, 50)),
       spread_bps=st.sampled_from([25.0, 150.0]))
@budget(8)
def test_m19_m20_drift_and_reinvestment_never_reprice_booked_contracts(
    scenario, drift_bps, spread_bps
):
    """M20: competitor/reference drift changes new-business pricing assumptions only; every
    surviving cohort keeps its booked rates. M19: the (unapplied) reinvestment spread leaves
    every figure unchanged."""
    def run(reinvestment: float):
        mp = MultiPeriodConfig(
            n_periods=3, loan_runoff_rate=0.3, deposit_runoff_rate=0.5, discount_rate=0.05,
            competitor_loan_rate_drift_bps=drift_bps, competitor_deposit_rate_drift_bps=drift_bps,
            market_cash_rate_drift_bps=abs(drift_bps),  # cash cannot drift below zero
            reinvestment_spread_bps=reinvestment,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return run_multiperiod_diagnostics(_engine_config(scenario, mp),
                                               max_boundary_expansions=0)

    base, with_spread = run(0.0), run(spread_bps)
    summary = base.cohort_summary()
    carried = summary[summary["period"] > summary["origination_period"]]
    assert not carried.empty
    for row in summary.itertuples(index=False):
        winner = base.period_diagnostics[row.origination_period].best_result
        assert (row.booked_loan_rate, row.booked_deposit_rate) == (
            winner.loan_rate, winner.deposit_rate
        )
    assert with_spread.npv == base.npv
    assert with_spread.period_summary().equals(base.period_summary())
