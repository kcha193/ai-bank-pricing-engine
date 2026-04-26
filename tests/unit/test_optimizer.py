import pytest
import numpy as np

from balance_pricing_engine.config import EngineConfig, load_config
from balance_pricing_engine.optimization.grid_search import GRID_RESULT_COLUMNS
from balance_pricing_engine.scenarios.runner import (
    build_configured_rate_grids,
    run_baseline_diagnostics,
    run_baseline_scenario,
)


def test_baseline_optimizer_returns_valid_joint_rates():
    config = load_config("configs/base.yaml")

    result = run_baseline_scenario(config)
    loan_grid, deposit_grid = build_configured_rate_grids(config)

    assert min(loan_grid) <= result.loan_rate <= max(loan_grid)
    assert min(deposit_grid) <= result.deposit_rate <= max(deposit_grid)
    assert result.expected_loan_volume > 0
    assert result.expected_deposit_volume > 0


def test_baseline_diagnostics_include_every_grid_candidate():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config)
    loan_grid, deposit_grid = build_configured_rate_grids(config)

    expected_loan_rates = _expected_spread_count(config.pricing_strategy.home_loan_spread_bps)
    expected_deposit_rates = _expected_spread_count(config.pricing_strategy.term_deposit_spread_bps)

    assert list(diagnostics.grid_results.columns) == GRID_RESULT_COLUMNS
    assert len(loan_grid) == expected_loan_rates
    assert len(deposit_grid) == expected_deposit_rates
    assert len(diagnostics.grid_results) == expected_loan_rates * expected_deposit_rates


def test_pricing_strategy_grid_takes_precedence_over_absolute_product_grid():
    raw_config = _market_relative_config_with_stale_absolute_grid()
    config = EngineConfig.model_validate(raw_config)

    loan_grid, deposit_grid = build_configured_rate_grids(config)
    diagnostics = run_baseline_diagnostics(config)

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
