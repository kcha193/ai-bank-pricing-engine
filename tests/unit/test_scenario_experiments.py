from types import SimpleNamespace

import pytest

from balance_pricing_engine.config import (
    BalanceSheetConfig,
    EngineConfig,
    MarketConfig,
    PricingStrategyConfig,
    ResponseConfig,
    SimulationConfig,
    SpreadGridConfigModel,
)
from balance_pricing_engine.scenarios import experiments


def make_config() -> EngineConfig:
    return EngineConfig(
        random_seed=7,
        simulation=SimulationConfig(n_loan_customers=10, n_deposit_customers=20),
        market=MarketConfig(
            cash_rate=0.04,
            wholesale_funding_spread=0.01,
            competitor_loan_rate=0.065,
            competitor_deposit_rate=0.045,
            home_loan_reference_rate=0.0525,
            term_deposit_reference_rate=0.04,
        ),
        balance_sheet=BalanceSheetConfig(
            required_deposit_funding_ratio=0.65,
            funding_shortfall_penalty_rate=0.018,
        ),
        pricing_strategy=PricingStrategyConfig(
            home_loan_spread_bps=SpreadGridConfigModel(min=-50, max=50, step=5),
            term_deposit_spread_bps=SpreadGridConfigModel(min=-40, max=40, step=5),
        ),
        response=ResponseConfig(
            loan_rate_elasticity=-85.0,
            loan_competitor_gap_elasticity=-65.0,
            deposit_rate_elasticity=95.0,
            deposit_competitor_gap_elasticity=75.0,
        ),
    )


def test_build_higher_funding_penalty_config_only_changes_penalty_rate():
    config = make_config()

    scenario_config = experiments.build_higher_funding_penalty_config(config, multiplier=2.0)

    assert scenario_config.balance_sheet.funding_shortfall_penalty_rate == 0.036
    assert config.balance_sheet.funding_shortfall_penalty_rate == 0.018
    assert scenario_config.market == config.market
    assert scenario_config.response == config.response


def test_run_higher_funding_penalty_scenario_runs_diagnostics_with_changed_config(monkeypatch):
    config = make_config()
    captured_penalties = []

    def fake_run_baseline_diagnostics(scenario_config):
        captured_penalties.append(scenario_config.balance_sheet.funding_shortfall_penalty_rate)
        return SimpleNamespace(best_result="scenario-result")

    monkeypatch.setattr(experiments, "run_baseline_diagnostics", fake_run_baseline_diagnostics)

    diagnostics = experiments.run_higher_funding_penalty_scenario(config, multiplier=1.5)

    assert diagnostics.best_result == "scenario-result"
    assert captured_penalties == [pytest.approx(0.027)]
