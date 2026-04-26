import numpy as np
import pandas as pd

from balance_pricing_engine.config import load_config
from balance_pricing_engine.diagnostics import (
    BOUNDARY_WARNING_CAUSES,
    SENSITIVITY_RESULT_COLUMNS,
    evaluate_boundary_diagnostics,
    expand_rate_grids_for_boundary,
    run_sensitivity_analysis,
)
from balance_pricing_engine.optimization.grid_search import OptimizationResult
from balance_pricing_engine.scenarios.runner import (
    build_configured_rate_grids,
    run_baseline_diagnostics,
)


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


def test_boundary_expansion_extends_only_hit_boundaries():
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

    loan_grid, deposit_grid, expanded = expand_rate_grids_for_boundary(
        result,
        loan_rate_grid=np.array([0.06, 0.07, 0.08]),
        deposit_rate_grid=np.array([0.03, 0.04, 0.05]),
    )

    assert expanded
    assert np.allclose(loan_grid, np.array([0.06, 0.07, 0.08, 0.09, 0.10]))
    assert np.allclose(deposit_grid, np.array([0.03, 0.04, 0.05]))


def test_baseline_diagnostics_runs_boundary_follow_up_before_accepting_result():
    config = load_config("configs/base.yaml")
    initial_loan_grid, initial_deposit_grid = build_configured_rate_grids(config)

    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=1)

    assert diagnostics.initial_boundary_diagnostics.has_warning
    assert diagnostics.expansion_rounds == 1
    assert diagnostics.grid_results["loan_rate"].max() > max(initial_loan_grid)
    assert diagnostics.grid_results["deposit_rate"].max() > max(initial_deposit_grid)


def test_sensitivity_runner_returns_expected_columns():
    config = load_config("configs/base.yaml")

    sensitivity = run_sensitivity_analysis(config)

    assert list(sensitivity.columns) == SENSITIVITY_RESULT_COLUMNS
    assert len(sensitivity) > 1
    assert sensitivity["boundary_warning_flag"].isin([True, False]).all()
    assert sensitivity["initial_boundary_warning_flag"].isin([True, False]).all()
    assert (sensitivity["expansion_rounds"] >= 0).all()


def test_default_sensitivity_includes_two_way_stresses():
    config = load_config("configs/base.yaml")

    sensitivity = run_sensitivity_analysis(config)

    assert "loan_elasticity_stronger__funding_penalty_higher" in set(
        sensitivity["scenario_name"]
    )
    assert "competitor_loan_rate_higher__wholesale_funding_rate_higher" in set(
        sensitivity["scenario_name"]
    )


def test_scenario_diagnostics_can_persist_regression_artifacts(tmp_path):
    config = load_config("configs/base.yaml")
    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=0)
    sensitivity = run_sensitivity_analysis(config).head(2)

    diagnostics.write_artifacts(tmp_path, sensitivity=sensitivity)

    grid_results = pd.read_csv(tmp_path / "grid_results.csv")
    top_results = pd.read_csv(tmp_path / "top_results.csv")
    saved_sensitivity = pd.read_csv(tmp_path / "sensitivity.csv")
    summary = (tmp_path / "summary.json").read_text(encoding="utf-8")

    assert len(grid_results) == len(diagnostics.grid_results)
    assert len(top_results) == 5
    assert len(saved_sensitivity) == 2
    assert '"best_loan_rate"' in summary
    assert '"expansion_rounds": 0' in summary
