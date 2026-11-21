"""Multi-period roll with explicit cohort accounting (FIN-002, Phase 3b).

Fake-period tests book a fixed new-business cohort each period and compute its standalone
objective at that period's market with an independent calculator (``_standalone_result``).
Expected multi-period figures are worked out by hand in each test.

Shared fake market (``make_config``): cash 4.00%, wholesale spread 1.00%, deposit funding
ratio 65%, shortfall penalty 1.80%, RWA 0.35 x tier-1 10% x hurdle 12% = 0.0042 capital per
dollar of loans. The standard fake cohort is loans 1000 at 6%, deposits 800 at 4%, ECL 2:
standalone objective 60 - 2 - 4.2 - 32 - 200 x 5% = 11.8.
"""

from __future__ import annotations

from pathlib import Path
import warnings

import pandas as pd
import pytest
from pydantic import ValidationError

from balance_pricing_engine.config import (
    BalanceSheetConfig,
    EngineConfig,
    GridConfig,
    MarketConfig,
    MultiPeriodConfig,
    PricingStrategyConfig,
    ProductConfig,
    ResponseConfig,
    SimulationConfig,
    SpreadGridConfigModel,
    load_config,
)
from balance_pricing_engine.optimization.grid_search import OptimizationResult
from balance_pricing_engine.scenarios import multiperiod as multiperiod_module
from balance_pricing_engine.scenarios.multiperiod import run_multiperiod_diagnostics
from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics

BASE_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "base.yaml"
CAPITAL_PER_DOLLAR = 0.35 * 0.10 * 0.12
STANDARD = dict(loan_rate=0.06, deposit_rate=0.04, loan_volume=1000.0, deposit_volume=800.0)

LEGACY_SUMMARY_COLUMNS = [
    "period",
    "best_loan_rate",
    "best_deposit_rate",
    "total_objective",
    "discounted_contribution",
]


# ---------------------------------------------------------------- real-engine checks


def test_period_count():
    config = make_config(MultiPeriodConfig(n_periods=3))

    diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert diagnostics.n_periods == 3
    assert len(diagnostics.period_diagnostics) == 3
    assert len(diagnostics.period_discounted_contributions) == 3
    assert len(diagnostics.period_economics) == 3


def test_npv_is_sum_of_discounted_contributions():
    config = make_config(MultiPeriodConfig(n_periods=3, discount_rate=0.05))

    diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert diagnostics.npv == pytest.approx(sum(diagnostics.period_discounted_contributions))


def test_t18_t19_t44_fin002_zero_runoff_back_book_matches_period_zero():
    """FIN-002 audit case: base config, 2 periods, zero runoff, discount and drift.

    Before Phase 3b the surviving period-0 book contributed about -$0.57m in period 1
    (loans repriced at the cash rate; ECL, capital and funding omitted) against a period-0
    objective of about +$5.92m. Identical new business plus an identical surviving book
    must double every component, because the funding terms are homogeneous of degree one.
    """
    config = load_config(BASE_CONFIG).model_copy(
        update={
            "multiperiod": MultiPeriodConfig(
                n_periods=2, loan_runoff_rate=0.0, deposit_runoff_rate=0.0, discount_rate=0.0
            )
        }
    )

    diagnostics = run_multiperiod_diagnostics(config)

    p0 = diagnostics.period_diagnostics[0].best_result
    e0, e1 = diagnostics.period_economics
    assert p0.total_objective > 5_000_000  # profitable new business, as audited
    assert e1.new_business_objective == pytest.approx(p0.total_objective, rel=1e-12)
    assert e1.back_book_incremental_contribution == pytest.approx(p0.total_objective, rel=1e-9)
    assert e1.active_portfolio_objective == pytest.approx(2 * p0.total_objective, rel=1e-9)
    assert e1.surviving_loan_volume == pytest.approx(p0.expected_loan_volume, rel=1e-12)
    assert e1.surviving_deposit_volume == pytest.approx(p0.expected_deposit_volume, rel=1e-12)
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
        assert getattr(e1, name) == pytest.approx(2 * getattr(p0, name), rel=1e-9), name
        assert getattr(e0, name) == pytest.approx(getattr(p0, name), rel=1e-9), name
    assert diagnostics.npv == pytest.approx(3 * p0.total_objective, rel=1e-9)


def test_t40_t41_repeated_runs_are_identical_and_cohorts_keep_vintages():
    config = make_config(
        MultiPeriodConfig(n_periods=3, loan_runoff_rate=0.3, deposit_runoff_rate=0.5)
    )

    first = run_multiperiod_diagnostics(config, max_boundary_expansions=0)
    second = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    pd.testing.assert_frame_equal(first.period_summary(), second.period_summary())
    pd.testing.assert_frame_equal(first.cohort_summary(), second.cohort_summary())
    assert [c.origination_period for c in first.booked_cohorts] == [0, 1, 2]
    period_2 = first.cohort_summary().query("period == 2")
    assert period_2["origination_period"].tolist() == [0, 1, 2]
    assert period_2["loan_survival"].tolist() == pytest.approx([0.49, 0.7, 1.0])
    assert period_2["deposit_survival"].tolist() == pytest.approx([0.25, 0.5, 1.0])


def test_t42_t43_summary_keeps_legacy_columns_and_attribution_reconciles():
    config = make_config(
        MultiPeriodConfig(n_periods=3, loan_runoff_rate=0.3, deposit_runoff_rate=0.5)
    )

    diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)
    summary = diagnostics.period_summary()

    assert list(summary.columns[: len(LEGACY_SUMMARY_COLUMNS)]) == LEGACY_SUMMARY_COLUMNS
    # total_objective keeps its meaning: the period's optimised new-business objective.
    assert summary["total_objective"].tolist() == [
        d.best_result.total_objective for d in diagnostics.period_diagnostics
    ]
    assert summary["total_objective"].tolist() == summary["new_business_objective"].tolist()
    pd.testing.assert_series_equal(
        summary["active_portfolio_objective"],
        summary["new_business_objective"] + summary["back_book_incremental_contribution"],
        check_names=False,
    )
    pd.testing.assert_series_equal(
        summary["total_active_loan_volume"],
        summary["new_loan_volume"] + summary["surviving_loan_volume"],
        check_names=False,
    )
    pd.testing.assert_series_equal(
        summary["total_active_deposit_volume"],
        summary["new_deposit_volume"] + summary["surviving_deposit_volume"],
        check_names=False,
    )
    assert summary["discounted_contribution"].tolist() == pytest.approx(
        (summary["active_portfolio_objective"] * summary["discount_factor"]).tolist()
    )
    assert summary.loc[0, "back_book_incremental_contribution"] == pytest.approx(0.0, abs=1e-6)
    assert summary.loc[0, "surviving_loan_volume"] == 0.0


def test_provenance_discloses_synthetic_response_and_assumptions():
    config = make_config(MultiPeriodConfig(n_periods=2, discount_rate=0.05))

    diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    provenance = diagnostics.provenance
    assert provenance["response_model"] == "synthetic"
    assert provenance["projection"] == "rolling_annual_scenario_projection"
    assert provenance["pricing_mode"] == "products"
    assert provenance["reference_rate_drift"] == "track_competitor"
    assert provenance["discount_convention"] == "end_of_period"
    assert len(provenance["config_hash"]) == 64


# ---------------------------------------------------------------- cohort roll with fakes


def test_t27_t28_t30_three_period_roll_reconciles_with_hand_calculation(monkeypatch):
    install_fake_periods(monkeypatch, [STANDARD, STANDARD, STANDARD])
    config = make_config(
        MultiPeriodConfig(
            n_periods=3, loan_runoff_rate=0.25, deposit_runoff_rate=0.50, discount_rate=0.10
        )
    )

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    # Period 1: loans 1000 + 750, deposits 800 + 400.
    #   105 - 3.5 - 7.35 - 48 - 550 x 5% (27.5) - 0 shortfall = 18.65
    # Period 2: loans 1000 + 750 + 562.5, deposits 800 + 400 + 200.
    #   138.75 - 4.625 - 9.7125 - 56 - 45.625 - (1503.125 - 1400) x 1.8% = 20.93125
    objectives = [e.active_portfolio_objective for e in result.period_economics]
    assert objectives == pytest.approx([11.8, 18.65, 20.93125])
    assert [e.back_book_incremental_contribution for e in result.period_economics] == (
        pytest.approx([0.0, 6.85, 9.13125], abs=1e-9)
    )
    assert [e.discount_factor for e in result.period_economics] == pytest.approx(
        [1 / 1.1, 1 / 1.21, 1 / 1.331]
    )
    assert result.period_discounted_contributions == pytest.approx(
        [11.8 / 1.1, 18.65 / 1.21, 20.93125 / 1.331]
    )
    assert result.npv == pytest.approx(11.8 / 1.1 + 18.65 / 1.21 + 20.93125 / 1.331)
    period_2 = result.period_economics[2]
    assert period_2.funding_shortfall == pytest.approx(103.125)
    assert period_2.surviving_loan_volume == pytest.approx(1312.5)
    assert period_2.surviving_deposit_volume == pytest.approx(600.0)


def test_t27_t29_zero_discount_two_period_npv_is_undiscounted_sum(monkeypatch):
    install_fake_periods(monkeypatch, [STANDARD, STANDARD])
    config = make_config(
        MultiPeriodConfig(
            n_periods=2, loan_runoff_rate=0.0, deposit_runoff_rate=0.0, discount_rate=0.0
        )
    )

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert [e.discount_factor for e in result.period_economics] == [1.0, 1.0]
    assert result.npv == pytest.approx(11.8 + 23.6)


def test_t20_full_runoff_leaves_no_back_book(monkeypatch):
    install_fake_periods(monkeypatch, [STANDARD, STANDARD, STANDARD])
    config = make_config(
        MultiPeriodConfig(n_periods=3, loan_runoff_rate=1.0, deposit_runoff_rate=1.0)
    )

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    for economics in result.period_economics:
        assert economics.surviving_loan_volume == 0.0
        assert economics.surviving_deposit_volume == 0.0
        assert economics.back_book_incremental_contribution == pytest.approx(0.0, abs=1e-9)
        assert economics.active_portfolio_objective == pytest.approx(11.8)
        assert economics.n_active_cohorts == 1


def test_t21_surviving_loans_without_surviving_deposits_need_wholesale_funding(monkeypatch):
    install_fake_periods(monkeypatch, [STANDARD, STANDARD])
    config = make_config(
        MultiPeriodConfig(
            n_periods=2, loan_runoff_rate=0.0, deposit_runoff_rate=1.0, discount_rate=0.0
        )
    )

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    # Loans 2000, deposits 800: 120 - 4 - 8.4 - 32 - 1200 x 5% - (1300 - 800) x 1.8% = 6.6
    period_1 = result.period_economics[1]
    assert period_1.active_portfolio_objective == pytest.approx(6.6)
    assert period_1.back_book_incremental_contribution == pytest.approx(6.6 - 11.8)
    assert period_1.wholesale_funding_cost == pytest.approx(60.0)
    assert period_1.funding_shortfall_penalty == pytest.approx(9.0)


def test_t08_t32_t33_cash_drift_moves_funding_not_contracted_rates(monkeypatch):
    def run(spread_bps: float):
        install_fake_periods(monkeypatch, [STANDARD, STANDARD])
        config = make_config(
            MultiPeriodConfig(
                n_periods=2,
                loan_runoff_rate=0.0,
                deposit_runoff_rate=0.0,
                discount_rate=0.0,
                market_cash_rate_drift_bps=100,
                competitor_loan_rate_drift_bps=100,
                competitor_deposit_rate_drift_bps=100,
                reinvestment_spread_bps=spread_bps,
            )
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            return run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    result = run(0.0)
    with_spread = run(100.0)

    # Period 1 market: cash 5%, wholesale 6%. Loans 2000 (both at booked 6%), deposits 1600.
    #   120 - 4 - 8.4 - 64 - 400 x 6% = 19.6; new business alone 60-2-4.2-32-200 x 6% = 9.8
    period_1 = result.period_economics[1]
    assert period_1.loan_interest_income == pytest.approx(2000.0 * 0.06)
    assert period_1.wholesale_funding_cost == pytest.approx(400.0 * 0.06)
    assert period_1.active_portfolio_objective == pytest.approx(19.6)
    assert period_1.new_business_objective == pytest.approx(9.8)
    assert period_1.back_book_incremental_contribution == pytest.approx(9.8)
    # A reinvestment spread has no rollover event to apply to, so it changes nothing.
    assert with_spread.npv == pytest.approx(result.npv)
    assert with_spread.reinvestment_spread_bps == 100.0


def test_reinvestment_spread_warns_that_it_is_not_applied(monkeypatch):
    install_fake_periods(monkeypatch, [STANDARD, STANDARD])
    config = make_config(MultiPeriodConfig(n_periods=2, reinvestment_spread_bps=50.0))

    with pytest.warns(UserWarning, match="reinvestment_spread_bps .* not applied"):
        result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert result.reinvestment_spread_bps == 50.0


def test_t31_tenor_years_overrides_loan_runoff_rate(monkeypatch):
    install_fake_periods(monkeypatch, [STANDARD, STANDARD])
    # loan_tenor_years=2 -> runoff 1/2; loan_runoff_rate=0.10 must be ignored.
    config = make_config(
        MultiPeriodConfig(n_periods=2, loan_runoff_rate=0.10, loan_tenor_years=2.0)
    )

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert result.loan_runoff_rate == pytest.approx(0.50)
    assert result.period_economics[1].surviving_loan_volume == pytest.approx(500.0)


def test_t31_tenor_derived_runoff_is_capped_at_one(monkeypatch):
    install_fake_periods(monkeypatch, [STANDARD, STANDARD])
    config = make_config(
        MultiPeriodConfig(n_periods=2, loan_tenor_years=0.5, deposit_runoff_rate=1.0)
    )

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert result.loan_runoff_rate == pytest.approx(1.0)
    assert result.period_economics[1].surviving_loan_volume == 0.0
    assert result.period_discounted_contributions[1] == pytest.approx(11.8 / 1.08**2)


def test_t22_t23_cohorts_with_different_rates_stay_distinct(monkeypatch):
    second = dict(loan_rate=0.07, deposit_rate=0.03, loan_volume=500.0, deposit_volume=100.0)
    install_fake_periods(monkeypatch, [STANDARD, second])
    config = make_config(
        MultiPeriodConfig(
            n_periods=2, loan_runoff_rate=0.0, deposit_runoff_rate=0.0, discount_rate=0.0
        )
    )

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert [(c.loan_rate, c.deposit_rate) for c in result.booked_cohorts] == [
        (0.06, 0.04),
        (0.07, 0.03),
    ]
    period_1 = result.period_economics[1]
    assert period_1.loan_interest_income == pytest.approx(60.0 + 35.0)
    assert period_1.deposit_interest_cost == pytest.approx(32.0 + 3.0)


def test_inconsistent_new_business_objective_is_rejected(monkeypatch):
    install_fake_periods(monkeypatch, [STANDARD], objective_offset=1.0)
    config = make_config(MultiPeriodConfig(n_periods=2))

    with pytest.raises(ValueError, match="does not reconcile"):
        run_multiperiod_diagnostics(config, max_boundary_expansions=0)


# ---------------------------------------------------------------- market drift


def test_cash_rate_drift_propagates():
    config = make_config(MultiPeriodConfig(n_periods=2, market_cash_rate_drift_bps=50))

    with pytest.warns(UserWarning, match="competitor rates are not drifted"):
        diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    period_0_market = diagnostics.period_diagnostics[0].market
    period_1_market = diagnostics.period_diagnostics[1].market
    assert period_1_market.cash_rate > period_0_market.cash_rate
    assert period_1_market.wholesale_funding_rate > period_0_market.wholesale_funding_rate
    assert diagnostics.period_economics[1].cash_rate == pytest.approx(0.045)
    assert diagnostics.period_economics[1].wholesale_funding_rate == pytest.approx(0.055)


def test_cash_rate_drift_warns_when_competitor_rates_are_frozen():
    config = make_config(MultiPeriodConfig(n_periods=2, market_cash_rate_drift_bps=50))

    with pytest.warns(UserWarning, match="competitor rates are not drifted"):
        run_multiperiod_diagnostics(config, max_boundary_expansions=0)


def test_t34_t35_t36_t37_competitor_and_reference_rates_drift_together(monkeypatch):
    seen = install_fake_periods(monkeypatch, [STANDARD, STANDARD, STANDARD])
    config = make_config(
        MultiPeriodConfig(
            n_periods=3,
            competitor_loan_rate_drift_bps=25,
            competitor_deposit_rate_drift_bps=-10,
        )
    )

    run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    markets = [c.market for c in seen]
    assert [m.competitor_loan_rate for m in markets] == pytest.approx([0.065, 0.0675, 0.07])
    assert [m.competitor_deposit_rate for m in markets] == pytest.approx([0.045, 0.044, 0.043])
    assert [m.home_loan_reference_rate for m in markets] == pytest.approx(
        [0.0525, 0.055, 0.0575]
    )
    assert [m.term_deposit_reference_rate for m in markets] == pytest.approx(
        [0.04, 0.039, 0.038]
    )
    assert [m.cash_rate for m in markets] == pytest.approx([0.04, 0.04, 0.04])


def test_hold_policy_keeps_independently_sourced_reference_rates(monkeypatch):
    seen = install_fake_periods(monkeypatch, [STANDARD, STANDARD])
    config = make_config(
        MultiPeriodConfig(
            n_periods=2,
            competitor_loan_rate_drift_bps=25,
            competitor_deposit_rate_drift_bps=25,
            reference_rate_drift="hold",
        )
    )

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert seen[1].market.competitor_loan_rate == pytest.approx(0.0675)
    assert seen[1].market.home_loan_reference_rate == pytest.approx(0.0525)
    assert seen[1].market.term_deposit_reference_rate == pytest.approx(0.04)
    assert result.provenance["reference_rate_drift"] == "hold"


def test_reference_rate_drift_policy_is_validated():
    with pytest.raises(ValidationError):
        MultiPeriodConfig(reference_rate_drift="sometimes")


def test_spread_grid_follows_drifted_reference_rate():
    config = make_strategy_config(
        MultiPeriodConfig(
            n_periods=2, competitor_loan_rate_drift_bps=50, competitor_deposit_rate_drift_bps=50
        )
    )

    diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    # The spread grid is reference +/- spread, so every period-1 rate moves by 50 bp.
    rates = [sorted(d.grid_results["loan_rate"].unique()) for d in diagnostics.period_diagnostics]
    assert rates[1] == pytest.approx([rate + 0.005 for rate in rates[0]])
    spreads = [
        sorted(d.grid_results["home_loan_spread_to_market_bps"].unique())
        for d in diagnostics.period_diagnostics
    ]
    assert spreads[1] == pytest.approx(spreads[0])


def test_t38_zero_drift_preserves_period_zero_market(monkeypatch):
    seen = install_fake_periods(monkeypatch, [STANDARD, STANDARD])
    config = make_config(MultiPeriodConfig(n_periods=2))

    run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert seen[0].market == config.market
    assert seen[1].market == config.market


def test_t39_drift_that_makes_a_market_rate_negative_fails_safely(monkeypatch):
    install_fake_periods(monkeypatch, [STANDARD, STANDARD, STANDARD])
    config = make_config(
        MultiPeriodConfig(
            n_periods=3,
            competitor_loan_rate_drift_bps=-100,
            competitor_deposit_rate_drift_bps=-300,
        )
    )

    with pytest.raises(ValueError, match="period 2.*competitor_deposit_rate.*negative"):
        run_multiperiod_diagnostics(config, max_boundary_expansions=0)


def test_t39_drifted_reference_that_makes_strategy_grid_negative_fails_revalidation():
    config = make_strategy_config(
        MultiPeriodConfig(
            n_periods=2, competitor_loan_rate_drift_bps=0, competitor_deposit_rate_drift_bps=-380
        )
    )

    # Period-1 deposit reference 0.2% minus the 30 bp spread floor: the period config's
    # revalidation (not the drift guard) rejects it.
    with pytest.raises(
        ValueError, match="term_deposit_spread_bps generates negative rates"
    ):
        run_multiperiod_diagnostics(config, max_boundary_expansions=0)


def test_no_distortion_warning_when_competitor_rates_also_drifted():
    config = make_config(
        MultiPeriodConfig(
            n_periods=2,
            market_cash_rate_drift_bps=25,
            competitor_loan_rate_drift_bps=25,
            competitor_deposit_rate_drift_bps=25,
        )
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        run_multiperiod_diagnostics(config, max_boundary_expansions=0)
    assert not any("Competitor gaps will shift mechanically" in str(w.message) for w in caught)


@pytest.mark.parametrize(("loan_drift", "deposit_drift"), [(25, 0), (0, 25)])
def test_distortion_warning_fires_for_partial_competitor_drift(loan_drift, deposit_drift):
    config = make_config(
        MultiPeriodConfig(
            n_periods=2,
            market_cash_rate_drift_bps=25,
            competitor_loan_rate_drift_bps=loan_drift,
            competitor_deposit_rate_drift_bps=deposit_drift,
        )
    )
    with pytest.warns(UserWarning, match="competitor rates are not drifted"):
        run_multiperiod_diagnostics(config, max_boundary_expansions=0)


# ---------------------------------------------------------------- config validation


def test_config_validation():
    with pytest.raises(ValidationError):
        MultiPeriodConfig(n_periods=1)
    with pytest.raises(ValidationError):
        MultiPeriodConfig(loan_runoff_rate=1.1)
    with pytest.raises(ValidationError):
        MultiPeriodConfig(deposit_runoff_rate=-0.1)
    with pytest.raises(ValidationError):
        MultiPeriodConfig(discount_rate=-0.01)


def test_multiperiod_config_defaults():
    config = MultiPeriodConfig()

    assert config.reinvestment_spread_bps == 0.0
    assert config.loan_tenor_years is None
    assert config.reference_rate_drift == "track_competitor"


def test_loan_tenor_years_must_be_positive():
    with pytest.raises(ValidationError):
        MultiPeriodConfig(loan_tenor_years=0.0)
    with pytest.raises(ValidationError):
        MultiPeriodConfig(loan_tenor_years=-1.0)


def test_missing_multiperiod_config_raises():
    config = make_config(multiperiod=None)

    with pytest.raises(ValueError, match="multiperiod"):
        run_multiperiod_diagnostics(config)


# ---------------------------------------------------------------- helpers


def make_config(multiperiod: MultiPeriodConfig | None) -> EngineConfig:
    return EngineConfig(
        random_seed=7,
        simulation=SimulationConfig(n_loan_customers=500, n_deposit_customers=500),
        market=MarketConfig(
            cash_rate=0.04,
            wholesale_funding_spread=0.01,
            competitor_loan_rate=0.065,
            competitor_deposit_rate=0.045,
            home_loan_reference_rate=0.0525,
            term_deposit_reference_rate=0.04,
        ),
        products=ProductConfig(
            loan_rate_grid=GridConfig(start=0.06, stop=0.061, step=0.001),
            deposit_rate_grid=GridConfig(start=0.04, stop=0.041, step=0.001),
        ),
        balance_sheet=BalanceSheetConfig(
            required_deposit_funding_ratio=0.65,
            funding_shortfall_penalty_rate=0.018,
        ),
        response=ResponseConfig(
            loan_rate_elasticity=-85.0,
            loan_competitor_gap_elasticity=-65.0,
            deposit_rate_elasticity=95.0,
            deposit_competitor_gap_elasticity=75.0,
        ),
        multiperiod=multiperiod,
    )


def make_strategy_config(multiperiod: MultiPeriodConfig) -> EngineConfig:
    payload = make_config(multiperiod).model_dump()
    payload["products"] = None
    payload["pricing_strategy"] = PricingStrategyConfig(
        home_loan_spread_bps=SpreadGridConfigModel(min=-30, max=30, step=10),
        term_deposit_spread_bps=SpreadGridConfigModel(min=-30, max=30, step=10),
    ).model_dump()
    return EngineConfig.model_validate(payload)


def _standalone_result(
    config: EngineConfig,
    *,
    loan_rate: float,
    deposit_rate: float,
    loan_volume: float,
    deposit_volume: float,
    objective_offset: float = 0.0,
) -> OptimizationResult:
    """Independent single-period calculator for a booked cohort at ``config.market``."""
    market = config.market
    wholesale_rate = market.cash_rate + market.wholesale_funding_spread
    income = loan_rate * loan_volume
    credit_loss = 0.002 * loan_volume
    capital = CAPITAL_PER_DOLLAR * loan_volume
    deposit_cost = deposit_rate * deposit_volume
    wholesale = max(loan_volume - deposit_volume, 0.0) * wholesale_rate
    excess = max(deposit_volume - loan_volume, 0.0) * market.cash_rate
    shortfall = max(0.65 * loan_volume - deposit_volume, 0.0)
    penalty = shortfall * 0.018
    return OptimizationResult(
        loan_rate=loan_rate,
        deposit_rate=deposit_rate,
        loan_interest_income=income,
        expected_credit_loss=credit_loss,
        capital_charge=capital,
        deposit_interest_cost=deposit_cost,
        wholesale_funding_cost=wholesale,
        excess_deposit_income=excess,
        funding_shortfall_penalty=penalty,
        total_objective=(
            income - credit_loss - capital - deposit_cost - wholesale - penalty + excess
        )
        + objective_offset,
        expected_loan_volume=loan_volume,
        expected_deposit_volume=deposit_volume,
        funding_shortfall=shortfall,
    )


def install_fake_periods(monkeypatch, cohorts: list[dict], objective_offset: float = 0.0):
    """Replace the single-period engine; return the period configs it was called with."""
    queue = list(cohorts)
    seen: list[EngineConfig] = []

    def fake_run_baseline_diagnostics(config, max_boundary_expansions):
        seen.append(config)
        result = _standalone_result(config, objective_offset=objective_offset, **queue.pop(0))
        return ScenarioDiagnostics(
            best_result=result,
            grid_results=pd.DataFrame(),
            boundary_diagnostics=None,
            initial_boundary_diagnostics=None,
        )

    monkeypatch.setattr(
        multiperiod_module, "run_baseline_diagnostics", fake_run_baseline_diagnostics
    )
    return seen


def test_example_script_columns_exist_in_period_summary(monkeypatch):
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "examples" / "run_multiperiod.py"
    spec = importlib.util.spec_from_file_location("run_multiperiod_example", path)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    install_fake_periods(monkeypatch, [STANDARD, STANDARD])

    summary = run_multiperiod_diagnostics(
        make_config(MultiPeriodConfig(n_periods=2)), max_boundary_expansions=0
    ).period_summary()

    assert set(example.SUMMARY_COLUMNS) <= set(summary.columns)
    assert set(example.COMPONENT_COLUMNS) <= set(summary.columns)


def test_segmented_roll_books_reconciled_cohorts():
    from balance_pricing_engine.config import (
        DepositTierConfig,
        LoanTierConfig,
        SegmentedPricingConfig,
    )

    payload = make_config(
        MultiPeriodConfig(n_periods=2, loan_runoff_rate=0.0, deposit_runoff_rate=0.0)
    ).model_dump()
    payload["products"] = None
    loan_grid = GridConfig(start=0.06, stop=0.061, step=0.001)
    deposit_grid = GridConfig(start=0.04, stop=0.041, step=0.001)
    payload["segmented_pricing"] = SegmentedPricingConfig(
        loan_tiers=[
            LoanTierConfig(name="low", lvr_max=0.7, rate_grid=loan_grid),
            LoanTierConfig(name="high", rate_grid=loan_grid),
        ],
        deposit_tiers=[DepositTierConfig(name="all", rate_grid=deposit_grid)],
    ).model_dump()
    config = EngineConfig.model_validate(payload)

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    p0 = result.period_diagnostics[0].best_result
    assert result.provenance["pricing_mode"] == "segmented_pricing"
    # Booked at the volume-weighted average tier rate, which reproduces the tier income.
    assert result.booked_cohorts[0].booked_loan_interest_income == pytest.approx(
        p0.loan_interest_income, rel=1e-9
    )
    assert result.period_economics[1].back_book_incremental_contribution == pytest.approx(
        p0.total_objective, rel=1e-9
    )


def test_build_period_config_requires_multiperiod():
    with pytest.raises(ValueError, match="multiperiod"):
        multiperiod_module._build_period_config(make_config(None), 0)
