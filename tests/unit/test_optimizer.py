import pytest

from balance_pricing_engine.config import load_config
from balance_pricing_engine.optimization.grid_search import GRID_RESULT_COLUMNS
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics, run_baseline_scenario


def test_baseline_optimizer_returns_valid_joint_rates():
    config = load_config("configs/base.yaml")

    result = run_baseline_scenario(config)

    assert config.products.loan_rate_grid.start <= result.loan_rate <= config.products.loan_rate_grid.stop
    assert (
        config.products.deposit_rate_grid.start
        <= result.deposit_rate
        <= config.products.deposit_rate_grid.stop
    )
    assert result.expected_loan_volume > 0
    assert result.expected_deposit_volume > 0


def test_baseline_diagnostics_include_every_grid_candidate():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config)

    expected_loan_rates = (
        round(
            (config.products.loan_rate_grid.stop - config.products.loan_rate_grid.start)
            / config.products.loan_rate_grid.step
        )
        + 1
    )
    expected_deposit_rates = (
        round(
            (config.products.deposit_rate_grid.stop - config.products.deposit_rate_grid.start)
            / config.products.deposit_rate_grid.step
        )
        + 1
    )

    assert list(diagnostics.grid_results.columns) == GRID_RESULT_COLUMNS
    assert len(diagnostics.grid_results) == expected_loan_rates * expected_deposit_rates


def test_baseline_best_result_matches_max_grid_objective():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config)
    max_objective = diagnostics.grid_results["total_objective"].max()

    assert diagnostics.best_result.objective_value == pytest.approx(max_objective)


def test_top_results_are_sorted_by_objective_descending():
    config = load_config("configs/base.yaml")

    diagnostics = run_baseline_diagnostics(config)
    top_results = diagnostics.top_results(5)

    assert top_results["total_objective"].is_monotonic_decreasing
