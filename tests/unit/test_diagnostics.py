import numpy as np

from balance_pricing_engine.config import load_config
from balance_pricing_engine.diagnostics import (
    BOUNDARY_WARNING_CAUSES,
    SENSITIVITY_RESULT_COLUMNS,
    evaluate_boundary_diagnostics,
    run_sensitivity_analysis,
)
from balance_pricing_engine.optimization.grid_search import OptimizationResult
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


def test_objective_decomposition_components_reconcile_to_total_objective():
    config = load_config("configs/base.yaml")

    grid_results = run_baseline_diagnostics(config).grid_results
    reconciled_objective = (
        grid_results["loan_interest_income"]
        - grid_results["expected_credit_loss"]
        - grid_results["deposit_interest_cost"]
        - grid_results["wholesale_funding_cost"]
        - grid_results["funding_shortfall_penalty"]
    )

    assert np.allclose(reconciled_objective, grid_results["total_objective"])


def test_boundary_diagnostics_trigger_when_optimum_is_at_grid_edge():
    result = OptimizationResult(
        loan_rate=0.08,
        deposit_rate=0.04,
        loan_interest_income=1_000_000,
        expected_credit_loss=50_000,
        deposit_interest_cost=100_000,
        wholesale_funding_cost=75_000,
        funding_shortfall_penalty=25_000,
        total_objective=750_000,
        expected_loan_volume=10_000_000,
        expected_deposit_volume=7_000_000,
        funding_shortfall=500_000,
    )

    diagnostics = evaluate_boundary_diagnostics(
        result,
        loan_rate_grid=np.array([0.06, 0.07, 0.08]),
        deposit_rate_grid=np.array([0.03, 0.04, 0.05]),
    )

    assert diagnostics.has_warning
    assert "home loan rate 8.00%" in diagnostics.warnings[0]
    assert "maximum grid boundary" in diagnostics.warnings[0]
    for cause in BOUNDARY_WARNING_CAUSES:
        assert cause in diagnostics.warnings[0]


def test_sensitivity_runner_returns_expected_columns():
    config = load_config("configs/base.yaml")

    sensitivity = run_sensitivity_analysis(config)

    assert list(sensitivity.columns) == SENSITIVITY_RESULT_COLUMNS
    assert len(sensitivity) > 1
    assert sensitivity["boundary_warning_flag"].isin([True, False]).all()
