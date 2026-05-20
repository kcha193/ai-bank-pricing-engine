from __future__ import annotations

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
from balance_pricing_engine.scenarios.multiperiod import run_multiperiod_diagnostics


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
