from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from balance_pricing_engine.config import (
    BalanceSheetConfig,
    EngineConfig,
    GridConfig,
    MarketConfig,
    MultiPeriodConfig,
    ProductConfig,
    ResponseConfig,
    SimulationConfig,
)
from balance_pricing_engine.optimization.grid_search import OptimizationResult
from balance_pricing_engine.scenarios import multiperiod as multiperiod_module
from balance_pricing_engine.scenarios.multiperiod import run_multiperiod_diagnostics
from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics


def test_period_count():
    config = make_config(MultiPeriodConfig(n_periods=3))

    diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert diagnostics.n_periods == 3
    assert len(diagnostics.period_diagnostics) == 3
    assert len(diagnostics.period_discounted_contributions) == 3


def test_npv_is_sum_of_discounted_contributions():
    config = make_config(MultiPeriodConfig(n_periods=3, discount_rate=0.05))

    diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert diagnostics.npv == pytest.approx(sum(diagnostics.period_discounted_contributions))


def test_discounting_and_back_book_runoff_are_applied_by_period(monkeypatch):
    period_results = [
        _fake_period_result_with_volume(
            total_objective=100.0,
            loan_income_after_losses=10.0,
            deposit_cost=2.0,
            loan_volume=250.0,
            deposit_volume=50.0,
        ),
        _fake_period_result_with_volume(
            total_objective=200.0,
            loan_income_after_losses=20.0,
            deposit_cost=4.0,
            loan_volume=500.0,
            deposit_volume=100.0,
        ),
        _fake_period_result_with_volume(
            total_objective=300.0,
            loan_income_after_losses=30.0,
            deposit_cost=6.0,
            loan_volume=750.0,
            deposit_volume=150.0,
        ),
    ]

    def fake_run_baseline_diagnostics(config, max_boundary_expansions):
        return period_results.pop(0)

    monkeypatch.setattr(
        multiperiod_module,
        "run_baseline_diagnostics",
        fake_run_baseline_diagnostics,
    )
    config = make_config(
        MultiPeriodConfig(
            n_periods=3,
            loan_runoff_rate=0.25,
            deposit_runoff_rate=0.50,
            discount_rate=0.10,
        )
    )

    diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert diagnostics.period_discounted_contributions == pytest.approx(
        [
            100.0 / 1.10,
            (200.0 + 10.0 * 0.75 - 2.0 * 0.50) / 1.10**2,
            (300.0 + 10.0 * 0.75**2 + 20.0 * 0.75 - 2.0 * 0.50**2 - 4.0 * 0.50)
            / 1.10**3,
        ]
    )


def test_cash_rate_drift_propagates():
    config = make_config(
        MultiPeriodConfig(
            n_periods=2,
            market_cash_rate_drift_bps=50,
        )
    )

    diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    period_0_market = diagnostics.period_diagnostics[0].market
    period_1_market = diagnostics.period_diagnostics[1].market
    assert period_0_market is not None
    assert period_1_market is not None
    assert period_1_market.cash_rate > period_0_market.cash_rate
    assert period_1_market.wholesale_funding_rate > period_0_market.wholesale_funding_rate


def test_cash_rate_drift_warns_when_competitor_rates_are_frozen():
    config = make_config(MultiPeriodConfig(n_periods=2, market_cash_rate_drift_bps=50))

    with pytest.warns(UserWarning, match="competitor rates are not drifted"):
        run_multiperiod_diagnostics(config, max_boundary_expansions=0)


def test_cash_rate_drift_warns_mentions_reinvestment_spread_when_both_are_set():
    config = make_config(
        MultiPeriodConfig(n_periods=2, market_cash_rate_drift_bps=50, reinvestment_spread_bps=100)
    )

    with pytest.warns(UserWarning, match="reinvestment"):
        run_multiperiod_diagnostics(config, max_boundary_expansions=0)


def test_runoff_reduces_back_book():
    loan_runoff_rate = 0.30
    deposit_runoff_rate = 0.50

    loan_fractions = [_surviving_fraction(loan_runoff_rate, period) for period in range(3)]
    deposit_fractions = [_surviving_fraction(deposit_runoff_rate, period) for period in range(3)]

    assert loan_fractions == pytest.approx([0.70, 0.49, 0.343])
    assert deposit_fractions == pytest.approx([0.50, 0.25, 0.125])
    assert loan_fractions[2] < loan_fractions[1] < loan_fractions[0]
    assert deposit_fractions[2] < deposit_fractions[1] < deposit_fractions[0]


def test_config_validation():
    with pytest.raises(ValidationError):
        MultiPeriodConfig(n_periods=1)

    with pytest.raises(ValidationError):
        MultiPeriodConfig(loan_runoff_rate=1.5)

    MultiPeriodConfig(loan_runoff_rate=0.0, deposit_runoff_rate=0.0)


def test_reinvestment_spread_bps_defaults_to_zero():
    cfg = MultiPeriodConfig(n_periods=2)
    assert cfg.reinvestment_spread_bps == 0.0


def test_loan_tenor_years_defaults_to_none():
    cfg = MultiPeriodConfig(n_periods=2)
    assert cfg.loan_tenor_years is None


def test_loan_tenor_years_must_be_positive():
    with pytest.raises(ValidationError):
        MultiPeriodConfig(n_periods=2, loan_tenor_years=0.0)

    with pytest.raises(ValidationError):
        MultiPeriodConfig(n_periods=2, loan_tenor_years=-1.0)


def test_loan_tenor_years_accepted_when_positive():
    cfg = MultiPeriodConfig(n_periods=2, loan_tenor_years=5.0)
    assert cfg.loan_tenor_years == 5.0


def test_reinvestment_spread_bps_negative_is_allowed():
    cfg = MultiPeriodConfig(n_periods=2, reinvestment_spread_bps=-25.0)
    assert cfg.reinvestment_spread_bps == -25.0


def test_tenor_years_overrides_loan_runoff_rate(monkeypatch):
    period_results = [
        _fake_period_result(total_objective=100.0, loan_income_after_losses=10.0, deposit_cost=2.0),
        _fake_period_result(total_objective=100.0, loan_income_after_losses=10.0, deposit_cost=2.0),
    ]

    def fake_run(config, max_boundary_expansions):
        return period_results.pop(0)

    monkeypatch.setattr(multiperiod_module, "run_baseline_diagnostics", fake_run)

    # loan_tenor_years=2 -> effective runoff = 1/2 = 0.50
    # loan_runoff_rate is set differently to confirm it is ignored
    config = make_config(
        MultiPeriodConfig(n_periods=2, loan_runoff_rate=0.10, loan_tenor_years=2.0)
    )
    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    # After period 0: back_book_loan_income = 10.0
    # Period 1 opens with: 10.0 * (1 - 0.50) = 5.0  (not 10.0 * (1-0.10) = 9.0)
    # Expected period 1 back_book_income = 5.0 - (back_book_deposit_cost surviving)
    assert result.loan_runoff_rate == pytest.approx(0.50)


def test_tenor_derived_runoff_is_capped_at_one(monkeypatch):
    period_results = [
        _fake_period_result_with_volume(
            total_objective=100.0,
            loan_income_after_losses=10.0,
            deposit_cost=2.0,
            loan_volume=200.0,
            deposit_volume=150.0,
        ),
        _fake_period_result_with_volume(
            total_objective=100.0,
            loan_income_after_losses=10.0,
            deposit_cost=2.0,
            loan_volume=200.0,
            deposit_volume=150.0,
        ),
    ]

    def fake_run(config, max_boundary_expansions):
        return period_results.pop(0)

    monkeypatch.setattr(multiperiod_module, "run_baseline_diagnostics", fake_run)
    config = make_config(MultiPeriodConfig(n_periods=2, loan_tenor_years=0.5))

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert result.loan_runoff_rate == pytest.approx(1.0)
    assert result.period_discounted_contributions[1] == pytest.approx((100.0 - 1.0) / 1.08**2)


def test_back_book_deposit_cost_carries_booked_rates(monkeypatch):
    period_results = [
        _fake_period_result_with_volume(
            total_objective=100.0,
            loan_income_after_losses=10.0,
            deposit_cost=12.0,
            loan_volume=250.0,
            deposit_volume=200.0,
        ),
        _fake_period_result_with_volume(
            total_objective=100.0,
            loan_income_after_losses=10.0,
            deposit_cost=12.0,
            loan_volume=250.0,
            deposit_volume=200.0,
        ),
    ]

    def fake_run(config, max_boundary_expansions):
        return period_results.pop(0)

    monkeypatch.setattr(multiperiod_module, "run_baseline_diagnostics", fake_run)
    config = make_config(
        MultiPeriodConfig(
            n_periods=2,
            loan_runoff_rate=0.25,
            deposit_runoff_rate=0.50,
        )
    )

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert result.period_discounted_contributions[1] == pytest.approx(
        (100.0 + 250.0 * 0.75 * 0.04 - 12.0 * 0.50) / 1.08**2
    )


def test_back_book_loan_reinvestment_uses_period_cash_rate(monkeypatch):
    observed_cash_rates = []
    period_results = [
        _fake_period_result_with_volume(
            total_objective=100.0,
            loan_income_after_losses=10.0,
            deposit_cost=0.0,
            loan_volume=250.0,
            deposit_volume=0.0,
        ),
        _fake_period_result_with_volume(
            total_objective=100.0,
            loan_income_after_losses=10.0,
            deposit_cost=0.0,
            loan_volume=250.0,
            deposit_volume=0.0,
        ),
    ]

    def fake_run(config, max_boundary_expansions):
        observed_cash_rates.append(config.market.cash_rate)
        return period_results.pop(0)

    monkeypatch.setattr(multiperiod_module, "run_baseline_diagnostics", fake_run)
    config = make_config(
        MultiPeriodConfig(
            n_periods=2,
            loan_runoff_rate=0.25,
            deposit_runoff_rate=0.50,
            market_cash_rate_drift_bps=100,
            reinvestment_spread_bps=25,
        )
    )

    with pytest.warns(UserWarning, match="competitor rates are not drifted"):
        result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert observed_cash_rates == pytest.approx([0.04, 0.05])
    assert result.period_discounted_contributions[1] == pytest.approx(
        (100.0 + 250.0 * 0.75 * 0.0525) / 1.08**2
    )


def test_reinvestment_spread_raises_back_book_income(monkeypatch):
    # When reinvestment_spread_bps=100, surviving back-book earns cash_rate + 1%
    # Period 0: loan_income = 10.0 added to back-book
    # Period 1: back-book survives at (1 - runoff) and earns reinvestment_rate
    #   reinvestment_rate = cash_rate(0.04) + 0.01 = 0.05
    #   surviving back-book VOLUME (from period 0) = best_result.expected_loan_volume * (1-runoff)
    # This test checks that period 1 discounted contribution differs from the zero-spread baseline.

    results_with_spread = [
        _fake_period_result_with_volume(
            total_objective=100.0,
            loan_income_after_losses=10.0,
            deposit_cost=2.0,
            loan_volume=200.0,
            deposit_volume=150.0,
        ),
        _fake_period_result_with_volume(
            total_objective=100.0,
            loan_income_after_losses=10.0,
            deposit_cost=2.0,
            loan_volume=200.0,
            deposit_volume=150.0,
        ),
    ]
    results_zero_spread = [
        _fake_period_result_with_volume(
            total_objective=100.0,
            loan_income_after_losses=10.0,
            deposit_cost=2.0,
            loan_volume=200.0,
            deposit_volume=150.0,
        ),
        _fake_period_result_with_volume(
            total_objective=100.0,
            loan_income_after_losses=10.0,
            deposit_cost=2.0,
            loan_volume=200.0,
            deposit_volume=150.0,
        ),
    ]

    def fake_with(config, max_boundary_expansions):
        return results_with_spread.pop(0)

    def fake_zero(config, max_boundary_expansions):
        return results_zero_spread.pop(0)

    config_with = make_config(
        MultiPeriodConfig(n_periods=2, reinvestment_spread_bps=100.0, loan_runoff_rate=0.30)
    )
    config_zero = make_config(
        MultiPeriodConfig(n_periods=2, reinvestment_spread_bps=0.0, loan_runoff_rate=0.30)
    )

    monkeypatch.setattr(multiperiod_module, "run_baseline_diagnostics", fake_with)
    result_with = run_multiperiod_diagnostics(config_with, max_boundary_expansions=0)

    monkeypatch.setattr(multiperiod_module, "run_baseline_diagnostics", fake_zero)
    result_zero = run_multiperiod_diagnostics(config_zero, max_boundary_expansions=0)

    assert result_with.npv > result_zero.npv


def test_reinvestment_spread_bps_exposed_on_diagnostics(monkeypatch):
    period_results = [
        _fake_period_result(total_objective=100.0, loan_income_after_losses=10.0, deposit_cost=2.0),
        _fake_period_result(total_objective=100.0, loan_income_after_losses=10.0, deposit_cost=2.0),
    ]

    def fake_run(config, max_boundary_expansions):
        return period_results.pop(0)

    monkeypatch.setattr(multiperiod_module, "run_baseline_diagnostics", fake_run)
    config = make_config(MultiPeriodConfig(n_periods=2, reinvestment_spread_bps=50.0))

    result = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert result.reinvestment_spread_bps == 50.0


def test_missing_multiperiod_config_raises():
    config = make_config(multiperiod=None)

    with pytest.raises(ValueError, match="multiperiod"):
        run_multiperiod_diagnostics(config)


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


def _surviving_fraction(runoff_rate: float, period: int) -> float:
    return (1 - runoff_rate) ** (period + 1)


def _fake_period_result(
    total_objective: float,
    loan_income_after_losses: float,
    deposit_cost: float,
) -> ScenarioDiagnostics:
    result = OptimizationResult(
        loan_rate=0.06,
        deposit_rate=0.04,
        loan_interest_income=loan_income_after_losses,
        expected_credit_loss=0.0,
        deposit_interest_cost=deposit_cost,
        wholesale_funding_cost=0.0,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=0.0,
        total_objective=total_objective,
        expected_loan_volume=0.0,
        expected_deposit_volume=0.0,
        funding_shortfall=0.0,
    )
    return ScenarioDiagnostics(
        best_result=result,
        grid_results=pd.DataFrame(),
        boundary_diagnostics=None,
        initial_boundary_diagnostics=None,
    )


def _fake_period_result_with_volume(
    total_objective: float,
    loan_income_after_losses: float,
    deposit_cost: float,
    loan_volume: float,
    deposit_volume: float,
) -> ScenarioDiagnostics:
    result = OptimizationResult(
        loan_rate=0.06,
        deposit_rate=0.04,
        loan_interest_income=loan_income_after_losses,
        expected_credit_loss=0.0,
        deposit_interest_cost=deposit_cost,
        wholesale_funding_cost=0.0,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=0.0,
        total_objective=total_objective,
        expected_loan_volume=loan_volume,
        expected_deposit_volume=deposit_volume,
        funding_shortfall=0.0,
    )
    return ScenarioDiagnostics(
        best_result=result,
        grid_results=pd.DataFrame(),
        boundary_diagnostics=None,
        initial_boundary_diagnostics=None,
    )


def test_competitor_rates_drift_with_explicit_drift_bps():
    config = make_config(
        MultiPeriodConfig(
            n_periods=2,
            market_cash_rate_drift_bps=25,
            competitor_loan_rate_drift_bps=25,
        )
    )

    diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    period_0_market = diagnostics.period_diagnostics[0].market
    period_1_market = diagnostics.period_diagnostics[1].market
    assert period_0_market is not None
    assert period_1_market is not None
    assert period_1_market.competitor_loan_rate == pytest.approx(
        period_0_market.competitor_loan_rate + 25 / 10_000
    )


def test_no_distortion_warning_when_competitor_rates_also_drifted():
    config = make_config(
        MultiPeriodConfig(
            n_periods=2,
            market_cash_rate_drift_bps=25,
            competitor_loan_rate_drift_bps=25,
            competitor_deposit_rate_drift_bps=25,
        )
    )

    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error", UserWarning)
        run_multiperiod_diagnostics(config, max_boundary_expansions=0)


def test_distortion_warning_still_fires_when_only_cash_rate_drifts():
    config = make_config(MultiPeriodConfig(n_periods=2, market_cash_rate_drift_bps=25))

    with pytest.warns(UserWarning, match="competitor rates are not drifted"):
        run_multiperiod_diagnostics(config, max_boundary_expansions=0)


def test_distortion_warning_fires_for_partial_competitor_drift():
    # loan drifts but deposit does not — deposit gap still shifts mechanically
    config = make_config(
        MultiPeriodConfig(
            n_periods=2,
            market_cash_rate_drift_bps=25,
            competitor_loan_rate_drift_bps=25,
            competitor_deposit_rate_drift_bps=0,
        )
    )
    with pytest.warns(UserWarning, match="competitor rates are not drifted"):
        run_multiperiod_diagnostics(config, max_boundary_expansions=0)


def test_distortion_warning_fires_when_only_deposit_competitor_drifts():
    # deposit drifts but loan does not — loan gap still shifts mechanically
    config = make_config(
        MultiPeriodConfig(
            n_periods=2,
            market_cash_rate_drift_bps=25,
            competitor_loan_rate_drift_bps=0,
            competitor_deposit_rate_drift_bps=25,
        )
    )
    with pytest.warns(UserWarning, match="competitor rates are not drifted"):
        run_multiperiod_diagnostics(config, max_boundary_expansions=0)


def test_zero_competitor_drift_preserves_current_behavior():
    config = make_config(
        MultiPeriodConfig(
            n_periods=2,
            competitor_loan_rate_drift_bps=0,
            competitor_deposit_rate_drift_bps=0,
        )
    )

    diagnostics = run_multiperiod_diagnostics(config, max_boundary_expansions=0)

    assert diagnostics is not None
    assert diagnostics.n_periods == 2
    assert len(diagnostics.period_diagnostics) == 2
    assert diagnostics.npv == pytest.approx(sum(diagnostics.period_discounted_contributions))
    period_0_market = diagnostics.period_diagnostics[0].market
    period_1_market = diagnostics.period_diagnostics[1].market
    assert period_0_market is not None
    assert period_1_market is not None
    assert period_1_market.competitor_loan_rate == pytest.approx(period_0_market.competitor_loan_rate)
    assert period_1_market.competitor_deposit_rate == pytest.approx(
        period_0_market.competitor_deposit_rate
    )
