import numpy as np
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
from balance_pricing_engine.reporting import (
    build_config_summary,
    build_objective_decomposition_table,
    build_rate_grid_table,
    format_result_table,
    fmt_bps,
    fmt_money,
    fmt_rate,
)


class DummyDecomposition:
    loan_interest_income = 100.0
    expected_credit_loss = 10.0
    deposit_interest_cost = 20.0
    wholesale_funding_cost = 30.0
    funding_shortfall_penalty = 5.0
    total_objective = 35.0


def test_formatters_handle_missing_and_numeric_values():
    assert fmt_rate(None) == "n/a"
    assert fmt_rate(0.0525) == "5.25%"
    assert fmt_bps(12.4) == "12 bps"
    assert fmt_money(1234567.8) == "$1,234,568"


def test_format_result_table_formats_known_rate_money_and_bps_columns():
    raw = pd.DataFrame(
        [
            {
                "loan_rate": 0.061,
                "objective_value": 1_250_000.0,
                "home_loan_spread_to_market_bps": 35.0,
                "unchanged": "keep",
            }
        ]
    )

    formatted = format_result_table(raw)

    assert formatted.loc[0, "loan_rate"] == "6.10%"
    assert formatted.loc[0, "objective_value"] == "$1,250,000"
    assert formatted.loc[0, "home_loan_spread_to_market_bps"] == "35 bps"
    assert formatted.loc[0, "unchanged"] == "keep"
    assert raw.loc[0, "loan_rate"] == 0.061


def test_build_config_summary_uses_beginner_assumption_rows():
    config = EngineConfig(
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

    summary = build_config_summary(config)

    assert list(summary.columns) == ["section", "assumption", "value"]
    assert summary.loc[summary["assumption"] == "cash rate", "value"].item() == "4.00%"
    assert summary.loc[summary["assumption"] == "loan customers", "value"].item() == 10


def test_rate_grid_and_decomposition_tables_match_notebook_shape():
    rate_grid = build_rate_grid_table(
        {"home loan": np.array([0.05, 0.055]), "term deposit": np.array([0.04])}
    )
    decomposition = build_objective_decomposition_table(DummyDecomposition())

    assert rate_grid.to_dict("records") == [
        {"grid": "home loan", "rate_decimal": 0.05, "rate_percent": "5.00%"},
        {"grid": "home loan", "rate_decimal": 0.055, "rate_percent": "5.50%"},
        {"grid": "term deposit", "rate_decimal": 0.04, "rate_percent": "4.00%"},
    ]
    assert decomposition["component"].tolist() == [
        "loan interest income",
        "expected credit loss",
        "deposit interest cost",
        "wholesale funding cost",
        "funding shortfall penalty",
        "total objective",
    ]
    assert decomposition["amount"].tolist() == [100.0, -10.0, -20.0, -30.0, -5.0, 35.0]
