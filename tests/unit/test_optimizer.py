import pytest
import numpy as np
import pandas as pd

from balance_pricing_engine.config import EngineConfig, load_config
from balance_pricing_engine.optimization.grid_search import (
    GRID_RESULT_COLUMNS,
    build_rate_grid,
    evaluate_joint_rate_grid,
    select_best_result,
)
from balance_pricing_engine.rate_grids import build_configured_rate_grids
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics
from balance_pricing_engine.synthetic.market import MarketState


def test_baseline_optimizer_returns_valid_joint_rates():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config)
    result = diagnostics.best_result

    assert diagnostics.grid_results["loan_rate"].min() <= result.loan_rate
    assert result.loan_rate <= diagnostics.grid_results["loan_rate"].max()
    assert diagnostics.grid_results["deposit_rate"].min() <= result.deposit_rate
    assert result.deposit_rate <= diagnostics.grid_results["deposit_rate"].max()
    assert result.expected_loan_volume > 0
    assert result.expected_deposit_volume > 0


@pytest.mark.parametrize(
    ("start", "stop", "step", "message"),
    [
        (0.03, 0.04, 0, "step"),
        (0.03, 0.04, -0.005, "step"),
        (0.04, 0.03, 0.005, "stop"),
    ],
)
def test_build_rate_grid_rejects_invalid_inputs(start, stop, step, message):
    with pytest.raises(ValueError, match=message):
        build_rate_grid(start=start, stop=stop, step=step)


def test_baseline_diagnostics_include_every_grid_candidate():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=0)
    loan_grid, deposit_grid = build_configured_rate_grids(config)

    expected_loan_rates = _expected_spread_count(config.pricing_strategy.home_loan_spread_bps)
    expected_deposit_rates = _expected_spread_count(config.pricing_strategy.term_deposit_spread_bps)

    assert list(diagnostics.grid_results.columns) == GRID_RESULT_COLUMNS
    assert len(loan_grid) == expected_loan_rates
    assert len(deposit_grid) == expected_deposit_rates
    assert len(diagnostics.grid_results) == expected_loan_rates * expected_deposit_rates
    assert sorted(diagnostics.grid_results["loan_rate"].unique()) == pytest.approx(loan_grid)
    assert sorted(diagnostics.grid_results["deposit_rate"].unique()) == pytest.approx(
        deposit_grid
    )


def test_pricing_strategy_grid_takes_precedence_over_absolute_product_grid():
    raw_config = _market_relative_config_with_stale_absolute_grid()
    config = EngineConfig.model_validate(raw_config)

    loan_grid, deposit_grid = build_configured_rate_grids(config)
    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=0)

    assert list(loan_grid) == pytest.approx([0.0520, 0.0525, 0.0530])
    assert list(deposit_grid) == pytest.approx([0.0395, 0.0400, 0.0405])
    assert diagnostics.grid_results["loan_rate"].min() == pytest.approx(0.0520)
    assert diagnostics.grid_results["loan_rate"].max() == pytest.approx(0.0530)
    assert diagnostics.grid_results["deposit_rate"].min() == pytest.approx(0.0395)
    assert diagnostics.grid_results["deposit_rate"].max() == pytest.approx(0.0405)
    assert 0.10 not in set(diagnostics.grid_results["loan_rate"])
    assert 0.08 not in set(diagnostics.grid_results["deposit_rate"])


def test_baseline_best_result_matches_max_grid_objective():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config)
    max_objective = diagnostics.grid_results["total_objective"].max()

    assert diagnostics.best_result.objective_value == pytest.approx(max_objective)


def test_select_best_result_uses_first_grid_row_when_objectives_tie():
    tied_results = np.array(
        [
            [0.050, 0.030, 0.0, 0.0, 100.0, 10.0, 5.0, 1.0, 1.0, 83.0, 1_000.0, 500.0, 250.0],
            [0.055, 0.035, 50.0, 50.0, 110.0, 20.0, 5.0, 1.0, 1.0, 83.0, 900.0, 600.0, 100.0],
        ],
        dtype=float,
    )
    grid_results = pd.DataFrame(tied_results, columns=GRID_RESULT_COLUMNS)

    result = select_best_result(grid_results)

    assert result.loan_rate == pytest.approx(0.050)
    assert result.deposit_rate == pytest.approx(0.030)


def test_monotonic_objective_selects_highest_loan_and_lowest_deposit_rates():
    loan_customers = pd.DataFrame(
        {
            "loan_amount": [1_000_000.0],
            "expected_loss_rate": [0.0],
            "risk_score": [0.0],
            "credit_score": [700.0],
            "lvr": [0.75],
        }
    )
    deposit_customers = pd.DataFrame(
        {
            "deposit_amount": [500_000.0],
            "rate_sensitivity": [0.0],
            "relationship_years": [0.0],
            "digital_engagement": [0.0],
        }
    )

    grid_results = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=MarketState(
            cash_rate=0.0,
            wholesale_funding_spread=0.0,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.03,
        ),
        loan_rate_grid=np.array([0.04, 0.05, 0.06]),
        deposit_rate_grid=np.array([0.02, 0.03, 0.04]),
        loan_rate_elasticity=0.0,
        loan_competitor_gap_elasticity=0.0,
        deposit_rate_elasticity=0.0,
        deposit_competitor_gap_elasticity=0.0,
        required_deposit_funding_ratio=0.0,
        funding_shortfall_penalty_rate=0.0,
    )

    result = select_best_result(grid_results)

    assert result.loan_rate == pytest.approx(0.06)
    assert result.deposit_rate == pytest.approx(0.02)


def test_optimizer_output_contains_spread_to_market_fields():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config)
    result = diagnostics.best_result

    assert "home_loan_spread_to_market_bps" in diagnostics.grid_results.columns
    assert "term_deposit_spread_to_market_bps" in diagnostics.grid_results.columns
    assert result.home_loan_spread_to_market_bps == pytest.approx(
        (result.loan_rate - config.market.home_loan_reference_rate) * 10_000
    )
    assert result.term_deposit_spread_to_market_bps == pytest.approx(
        (result.deposit_rate - config.market.term_deposit_reference_rate) * 10_000
    )
    assert np.allclose(
        diagnostics.grid_results["home_loan_spread_to_market_bps"],
        (diagnostics.grid_results["loan_rate"] - config.market.home_loan_reference_rate)
        * 10_000,
    )
    assert np.allclose(
        diagnostics.grid_results["term_deposit_spread_to_market_bps"],
        (diagnostics.grid_results["deposit_rate"] - config.market.term_deposit_reference_rate)
        * 10_000,
    )


def test_top_results_are_sorted_by_objective_descending():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config)
    top_results = diagnostics.top_results(5)

    assert top_results["total_objective"].is_monotonic_decreasing


def _expected_spread_count(spread_config) -> int:
    return round((spread_config.max - spread_config.min) / spread_config.step) + 1


def _market_relative_config_with_stale_absolute_grid() -> dict:
    return {
        "random_seed": 42,
        "simulation": {
            "n_loan_customers": 10,
            "n_deposit_customers": 10,
        },
        "market": {
            "cash_rate": 0.0425,
            "wholesale_funding_spread": 0.012,
            "competitor_loan_rate": 0.0675,
            "competitor_deposit_rate": 0.045,
            "home_loan_reference_rate": 0.0525,
            "term_deposit_reference_rate": 0.0400,
        },
        "products": {
            "loan_rate_grid": {"start": 0.10, "stop": 0.11, "step": 0.01},
            "deposit_rate_grid": {"start": 0.08, "stop": 0.09, "step": 0.01},
        },
        "pricing_strategy": {
            "home_loan_spread_bps": {"min": -5, "max": 5, "step": 5},
            "term_deposit_spread_bps": {"min": -5, "max": 5, "step": 5},
        },
        "balance_sheet": {
            "required_deposit_funding_ratio": 0.65,
            "funding_shortfall_penalty_rate": 0.018,
        },
        "response": {
            "loan_rate_elasticity": -85.0,
            "loan_competitor_gap_elasticity": -65.0,
            "deposit_rate_elasticity": 95.0,
            "deposit_competitor_gap_elasticity": 75.0,
        },
    }
