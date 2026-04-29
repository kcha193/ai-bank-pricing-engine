import pandas as pd

from balance_pricing_engine.config import (
    BalanceSheetConfig,
    EngineConfig,
    MarketConfig,
    PricingStrategyConfig,
    ResponseConfig,
    SimulationConfig,
    SpreadGridConfigModel,
)
from balance_pricing_engine.modelling import workflows


def make_config() -> EngineConfig:
    return EngineConfig(
        random_seed=42,
        simulation=SimulationConfig(n_loan_customers=3, n_deposit_customers=4),
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


def test_run_response_model_workflow_builds_datasets_and_reports(monkeypatch):
    config = make_config()
    calls = []

    monkeypatch.setattr(
        workflows,
        "generate_loan_customers",
        lambda n_customers, rng: pd.DataFrame({"loan_customer": range(n_customers)}),
    )
    monkeypatch.setattr(
        workflows,
        "generate_deposit_customers",
        lambda n_customers, rng: pd.DataFrame({"deposit_customer": range(n_customers)}),
    )
    monkeypatch.setattr(
        workflows,
        "build_configured_rate_grids",
        lambda loaded_config: ([0.05, 0.06], [0.03, 0.04]),
    )
    monkeypatch.setattr(
        workflows,
        "build_loan_response_dataset",
        lambda *args, **kwargs: pd.DataFrame({"loan_response": [1, 0]}),
    )
    monkeypatch.setattr(
        workflows,
        "build_deposit_response_dataset",
        lambda *args, **kwargs: pd.DataFrame({"deposit_response": [0, 1]}),
    )

    def fake_train_response_model_report(**kwargs):
        calls.append(kwargs)
        return {"title": kwargs["title"], "metrics": {"auc": 1.0}}

    monkeypatch.setattr(
        workflows,
        "train_response_model_report",
        fake_train_response_model_report,
    )

    result = workflows.run_response_model_workflow(config)

    assert result.loan_grid == [0.05, 0.06]
    assert result.deposit_grid == [0.03, 0.04]
    assert result.loan_dataset.to_dict("list") == {"loan_response": [1, 0]}
    assert result.deposit_dataset.to_dict("list") == {"deposit_response": [0, 1]}
    assert [call["title"] for call in calls] == [
        "Home loan acceptance model",
        "Term deposit uptake model",
    ]
    assert calls[0]["expected_direction"] == "decreasing"
    assert calls[1]["expected_direction"] == "increasing"
    assert result.loan_model_report["metrics"] == {"auc": 1.0}
