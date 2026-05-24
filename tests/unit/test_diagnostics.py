import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import balance_pricing_engine.diagnostics as diagnostics_module
from balance_pricing_engine.config import load_config
from balance_pricing_engine.diagnostics import (
    ObjectiveDecomposition,
    SENSITIVITY_RESULT_COLUMNS,
    evaluate_adaptive_joint_rate_grid,
    evaluate_boundary_diagnostics,
    expand_rate_grids_for_boundary,
    infer_boundary_warning_cause,
    run_sensitivity_analysis,
    sensitivity_result_columns,
)
from balance_pricing_engine.optimization.grid_search import OptimizationResult
from balance_pricing_engine.synthetic.market import MarketState
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
        - grid_results["capital_charge"]
        - grid_results["deposit_interest_cost"]
        - grid_results["wholesale_funding_cost"]
        - grid_results["funding_shortfall_penalty"]
        + grid_results["excess_deposit_income"]
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
        excess_deposit_income=0.0,
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
    assert "Likely cause: loan income after losses dominates funding penalty." in (
        diagnostics.warnings[0]
    )


def test_boundary_warning_heuristic_categorises_each_cause_branch():
    base = ObjectiveDecomposition(
        loan_interest_income=1_000_000,
        expected_credit_loss=100_000,
        deposit_interest_cost=100_000,
        wholesale_funding_cost=50_000,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=50_000,
        total_objective=700_000,
        expected_loan_volume=10_000_000,
        expected_deposit_volume=6_000_000,
        funding_shortfall=500_000,
    )

    assert (
        infer_boundary_warning_cause(
            base,
            loan_rate=0.08,
            deposit_rate=0.04,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.04,
        )
        == "winner is far from competitor rates"
    )
    assert (
        infer_boundary_warning_cause(
            base,
            loan_rate=0.051,
            deposit_rate=0.039,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.04,
        )
        == "loan income after losses dominates funding penalty"
    )
    penalty_dominant = ObjectiveDecomposition(
        loan_interest_income=100_000,
        expected_credit_loss=10_000,
        deposit_interest_cost=100_000,
        wholesale_funding_cost=50_000,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=200_000,
        total_objective=-260_000,
        expected_loan_volume=10_000_000,
        expected_deposit_volume=6_000_000,
        funding_shortfall=500_000,
    )
    assert (
        infer_boundary_warning_cause(
            penalty_dominant,
            loan_rate=0.051,
            deposit_rate=0.039,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.04,
        )
        == "funding penalty dominates loan income after losses"
    )
    funding_near_target = ObjectiveDecomposition(
        loan_interest_income=1_000_000,
        expected_credit_loss=100_000,
        deposit_interest_cost=100_000,
        wholesale_funding_cost=50_000,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=50_000,
        total_objective=700_000,
        expected_loan_volume=10_000_000,
        expected_deposit_volume=6_000_000,
        funding_shortfall=50_000,
    )
    assert (
        infer_boundary_warning_cause(
            funding_near_target,
            loan_rate=0.051,
            deposit_rate=0.039,
            competitor_loan_rate=0.05,
            competitor_deposit_rate=0.04,
        )
        == "funding shortfall is near zero"
    )


def test_boundary_expansion_extends_only_hit_boundaries():
    result = OptimizationResult(
        loan_rate=0.08,
        deposit_rate=0.04,
        loan_interest_income=1_000_000,
        expected_credit_loss=50_000,
        deposit_interest_cost=100_000,
        wholesale_funding_cost=75_000,
        excess_deposit_income=0.0,
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


def test_boundary_expansion_handles_zero_clamp_without_invalid_grid_alignment():
    result = OptimizationResult(
        loan_rate=0.003,
        deposit_rate=0.03,
        loan_interest_income=0.0,
        expected_credit_loss=0.0,
        deposit_interest_cost=0.0,
        wholesale_funding_cost=0.0,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=0.0,
        total_objective=0.0,
        expected_loan_volume=0.0,
        expected_deposit_volume=0.0,
        funding_shortfall=0.0,
    )

    loan_grid, _, expanded = expand_rate_grids_for_boundary(
        result,
        loan_rate_grid=np.array([0.003, 0.008, 0.013]),
        deposit_rate_grid=np.array([0.02, 0.03, 0.04]),
    )

    assert not expanded
    assert np.allclose(loan_grid, np.array([0.003, 0.008, 0.013]))


def test_baseline_diagnostics_runs_boundary_follow_up_before_accepting_result():
    config = load_config("configs/base.yaml")
    initial_loan_grid, initial_deposit_grid = build_configured_rate_grids(config)

    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=1)

    assert diagnostics.initial_boundary_diagnostics.has_warning
    assert diagnostics.expansion_rounds == 1
    assert diagnostics.grid_results["loan_rate"].max() > max(initial_loan_grid)
    assert diagnostics.grid_results["deposit_rate"].max() > max(initial_deposit_grid)
    assert not diagnostics.boundary_diagnostics.has_warning


def test_adaptive_grid_expands_when_boundary_hit():
    from balance_pricing_engine.config import SpreadGridConfigModel

    config = load_config("configs/base.yaml")
    narrow_config = config.model_copy(
        update={
            "pricing_strategy": config.pricing_strategy.model_copy(
                update={
                    "home_loan_spread_bps": SpreadGridConfigModel(min=-5, max=5, step=5),
                    "term_deposit_spread_bps": SpreadGridConfigModel(min=-5, max=5, step=5),
                }
            )
        }
    )
    result = run_baseline_diagnostics(narrow_config)
    assert result.initial_boundary_diagnostics.has_warning
    assert result.expansion_rounds >= 1


def test_capped_adaptive_expansion_preserves_final_boundary_warning():
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
            "deposit_amount": [1_000.0],
            "rate_sensitivity": [0.0],
            "relationship_years": [0.0],
            "digital_engagement": [0.0],
        }
    )

    diagnostics = evaluate_adaptive_joint_rate_grid(
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
        max_boundary_expansions=0,
    )

    assert diagnostics.expansion_rounds == 0
    assert diagnostics.expansion_capped
    assert diagnostics.initial_boundary_diagnostics.has_warning
    assert diagnostics.boundary_diagnostics.has_warning
    assert diagnostics.best_result.loan_rate == 0.06
    assert diagnostics.best_result.deposit_rate == 0.02


def test_capped_expansion_flag_is_persisted_in_summary_json(tmp_path):
    config = load_config("configs/base.yaml")
    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=0)

    diagnostics.write_artifacts(tmp_path)

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["expansion_capped"] is True


def test_policy_band_breach_flag_is_persisted_when_expansion_wins_outside_initial_band(
    tmp_path,
):
    config = load_config("configs/base.yaml")
    diagnostics = run_baseline_diagnostics(config)

    diagnostics.write_artifacts(tmp_path)

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert diagnostics.expansion_rounds > 0
    assert diagnostics.policy_band_breached
    assert summary["policy_band_breached"] is True


def test_sensitivity_runner_returns_expected_columns():
    config = load_config("configs/base.yaml")

    sensitivity = run_sensitivity_analysis(config)

    assert list(sensitivity.columns) == SENSITIVITY_RESULT_COLUMNS
    assert len(sensitivity) > 1
    assert sensitivity["boundary_warning_flag"].isin([True, False]).all()
    assert sensitivity["initial_boundary_warning_flag"].isin([True, False]).all()
    assert (sensitivity["expansion_rounds"] >= 0).all()
    assert {
        "delta_loan_rate_bps",
        "delta_deposit_rate_bps",
        "delta_objective_pct",
        "parameter_family",
    } <= set(sensitivity.columns)
    assert sensitivity["parameter_family"].isin(
        ["baseline", "elasticity", "funding", "competitor", "wholesale", "other"]
    ).all()
    assert (sensitivity["delta_objective_pct"].abs() > 0).any()


def test_sensitivity_result_includes_excess_deposit_income():
    config = load_config("configs/base.yaml")
    sensitivity = run_sensitivity_analysis(config)
    assert "excess_deposit_income" in sensitivity.columns
    assert (sensitivity["excess_deposit_income"] >= 0).all()


def test_sensitivity_applies_configured_maturity_adjustment_to_case_rwa_weight(monkeypatch):
    config = load_config("configs/base.yaml")
    balance_sheet = config.balance_sheet.model_copy(update={"average_loan_tenor_years": 3.0})
    config = config.model_copy(update={"balance_sheet": balance_sheet})
    cases = [
        diagnostics_module.SensitivityCase(
            name="baseline",
            loan_rate_elasticity=config.response.loan_rate_elasticity,
            loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
            deposit_rate_elasticity=config.response.deposit_rate_elasticity,
            deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
            funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate,
            loan_rwa_weight=0.35,
            capital_hurdle_rate=config.balance_sheet.capital_hurdle_rate,
            wholesale_funding_rate=config.market.cash_rate
            + config.market.wholesale_funding_spread,
            competitor_loan_rate=config.market.competitor_loan_rate,
            competitor_deposit_rate=config.market.competitor_deposit_rate,
            response_coefficients=config.response.coefficients,
        ),
        diagnostics_module.SensitivityCase(
            name="rwa_stress",
            loan_rate_elasticity=config.response.loan_rate_elasticity,
            loan_competitor_gap_elasticity=config.response.loan_competitor_gap_elasticity,
            deposit_rate_elasticity=config.response.deposit_rate_elasticity,
            deposit_competitor_gap_elasticity=config.response.deposit_competitor_gap_elasticity,
            funding_shortfall_penalty_rate=config.balance_sheet.funding_shortfall_penalty_rate,
            loan_rwa_weight=0.70,
            capital_hurdle_rate=config.balance_sheet.capital_hurdle_rate,
            wholesale_funding_rate=config.market.cash_rate
            + config.market.wholesale_funding_spread,
            competitor_loan_rate=config.market.competitor_loan_rate,
            competitor_deposit_rate=config.market.competitor_deposit_rate,
            response_coefficients=config.response.coefficients,
        ),
    ]
    observed_rwa_weights = []

    def fake_evaluate_adaptive_joint_rate_grid(**kwargs):
        observed_rwa_weights.append(kwargs["rwa_weight"])
        result = OptimizationResult(
            loan_rate=0.06,
            deposit_rate=0.04,
            loan_interest_income=100.0,
            expected_credit_loss=10.0,
            capital_charge=4.0,
            deposit_interest_cost=20.0,
            wholesale_funding_cost=5.0,
            excess_deposit_income=0.0,
            funding_shortfall_penalty=1.0,
            total_objective=60.0,
            expected_loan_volume=1_000.0,
            expected_deposit_volume=700.0,
            funding_shortfall=0.0,
        )
        return SimpleNamespace(
            best_result=result,
            initial_boundary_diagnostics=SimpleNamespace(has_warning=False),
            boundary_diagnostics=SimpleNamespace(has_warning=False),
            expansion_rounds=0,
        )

    monkeypatch.setattr(
        diagnostics_module,
        "evaluate_adaptive_joint_rate_grid",
        fake_evaluate_adaptive_joint_rate_grid,
    )

    diagnostics_module._run_sensitivity_analysis_for_seed(
        config=config,
        cases=cases,
        seed=config.random_seed,
        max_boundary_expansions=0,
    )

    assert observed_rwa_weights == pytest.approx([0.35 * 1.12, 0.70 * 1.12])


def test_sensitivity_multi_seed_columns_match_contract():
    config = load_config("configs/base.yaml")
    df = run_sensitivity_analysis(config, n_seeds=3)
    expected_cols = sensitivity_result_columns(n_seeds=3)
    assert list(df.columns) == expected_cols


def test_default_sensitivity_includes_two_way_stresses():
    config = load_config("configs/base.yaml")

    sensitivity = run_sensitivity_analysis(config)
    scenario_names = set(sensitivity["scenario_name"])

    assert any(
        "elasticity" in scenario_name and "__" not in scenario_name
        for scenario_name in scenario_names
    )
    assert any(
        "funding" in scenario_name and "__" not in scenario_name
        for scenario_name in scenario_names
    )
    assert "loan_elasticity_stronger__funding_penalty_higher" in scenario_names
    assert "competitor_loan_rate_higher__wholesale_funding_rate_higher" in scenario_names
    assert "competitor_deposit_rate_lower__funding_penalty_higher" in scenario_names


def test_default_sensitivity_includes_gap_elasticity_and_feature_coefficient_stresses():
    config = load_config("configs/base.yaml")

    sensitivity = run_sensitivity_analysis(config)
    scenario_names = set(sensitivity["scenario_name"])

    expected = {
        "loan_competitor_gap_elasticity_weaker",
        "loan_competitor_gap_elasticity_stronger",
        "deposit_competitor_gap_elasticity_weaker",
        "deposit_competitor_gap_elasticity_stronger",
        "loan_risk_score_coef_weaker",
        "loan_risk_score_coef_stronger",
        "deposit_rate_sensitivity_coef_weaker",
        "deposit_rate_sensitivity_coef_stronger",
    }
    assert expected <= scenario_names
    assert set(
        sensitivity.loc[sensitivity["scenario_name"].isin(expected), "parameter_family"]
    ) == {"elasticity"}


def test_sensitivity_analysis_can_average_over_multiple_customer_seeds():
    config = load_config("configs/base.yaml")

    sensitivity = run_sensitivity_analysis(config, n_seeds=3)

    assert "objective_value_mean" in sensitivity.columns
    assert "objective_value_std_error" in sensitivity.columns
    assert sensitivity["objective_value_mean"].notna().all()
    assert (sensitivity["objective_value_std_error"] >= 0).all()


def test_scenario_diagnostics_can_persist_regression_artifacts(tmp_path):
    config = load_config("configs/base.yaml")
    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=0)
    sensitivity = run_sensitivity_analysis(config).head(2)

    diagnostics.write_artifacts(tmp_path, sensitivity=sensitivity)

    grid_results = pd.read_csv(tmp_path / "grid_results.csv")
    top_results = pd.read_csv(tmp_path / "top_results.csv")
    saved_sensitivity = pd.read_csv(tmp_path / "sensitivity.csv")
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))

    assert len(grid_results) == len(diagnostics.grid_results)
    assert len(top_results) == 5
    assert len(saved_sensitivity) == 2
    assert {
        "best_loan_rate",
        "best_deposit_rate",
        "objective_value",
        "capital_charge",
        "excess_deposit_income",
        "boundary_warning_flag",
        "initial_boundary_warning_flag",
        "expansion_rounds",
        "expansion_capped",
        "policy_band_breached",
    } <= set(summary)
    assert summary["best_loan_rate"] == diagnostics.best_result.loan_rate
    assert summary["best_deposit_rate"] == diagnostics.best_result.deposit_rate
    assert summary["objective_value"] == diagnostics.best_result.total_objective
    assert summary["capital_charge"] == diagnostics.best_result.capital_charge
    assert summary["boundary_warning_flag"] is diagnostics.boundary_diagnostics.has_warning
    assert (
        summary["initial_boundary_warning_flag"]
        is diagnostics.initial_boundary_diagnostics.has_warning
    )
    assert summary["expansion_rounds"] == 0
    assert summary["expansion_capped"] is diagnostics.expansion_capped
    assert summary["policy_band_breached"] is diagnostics.policy_band_breached


def test_counterfactual_decompositions_return_reconciled_top_rows(tmp_path):
    config = load_config("configs/base.yaml")
    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=0)

    decompositions = diagnostics.counterfactual_decompositions(top_n=2)
    diagnostics.write_artifacts(tmp_path, top_n=2)

    assert len(decompositions) == 2
    for decomposition in decompositions:
        assert decomposition.total_objective == pytest.approx(
            decomposition.reconciled_total_objective
        )
    saved = pd.read_csv(tmp_path / "counterfactuals.csv")
    assert len(saved) == 2
    assert "total_objective" in saved.columns


def test_cohort_attribution_reconciles_segment_volumes(tmp_path):
    config = load_config("configs/base.yaml")
    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=0)

    attribution = diagnostics.cohort_attribution()
    diagnostics.write_artifacts(tmp_path)

    assert set(attribution["loan_volume_by_risk_segment"]) == {"low", "medium", "high"}
    assert set(attribution["deposit_volume_by_wealth_segment"]) == {
        "low",
        "medium",
        "high",
    }
    assert sum(attribution["loan_volume_by_risk_segment"].values()) == pytest.approx(
        diagnostics.best_result.expected_loan_volume
    )
    assert sum(attribution["deposit_volume_by_wealth_segment"].values()) == pytest.approx(
        diagnostics.best_result.expected_deposit_volume
    )
    saved = pd.read_csv(tmp_path / "cohort_attribution.csv")
    assert {"segment_type", "segment", "expected_volume"} <= set(saved.columns)


def test_parameter_family_baseline() -> None:
    from balance_pricing_engine.diagnostics import _parameter_family

    assert _parameter_family("baseline") == "baseline"


def test_parameter_family_unknown_returns_other() -> None:
    from balance_pricing_engine.diagnostics import _parameter_family

    assert _parameter_family("some_unknown_scenario") == "other"


def test_boundary_expansion_span_multiplier_returns_one_for_default_and_above() -> None:
    from balance_pricing_engine.diagnostics import (
        MIN_BOUNDARY_EXPANSION_HORIZON,
        _boundary_expansion_span_multiplier,
    )

    # For max_boundary_expansions >= MIN_BOUNDARY_EXPANSION_HORIZON, multi-round 1×
    # expansion provides sufficient coverage; no multiplier is applied.
    for n in range(MIN_BOUNDARY_EXPANSION_HORIZON, MIN_BOUNDARY_EXPANSION_HORIZON + 4):
        assert _boundary_expansion_span_multiplier(n) == 1.0, n

    # Edge cases.
    assert _boundary_expansion_span_multiplier(0) == 1.0
    assert _boundary_expansion_span_multiplier(-1) == 1.0


def test_boundary_expansion_span_multiplier_compensates_for_tight_cap() -> None:
    from balance_pricing_engine.diagnostics import (
        MIN_BOUNDARY_EXPANSION_HORIZON,
        _boundary_expansion_span_multiplier,
    )

    # For max_boundary_expansions=1 and MIN_BOUNDARY_EXPANSION_HORIZON=2, one-shot
    # expansion must cover the same cumulative span as 2 rounds of 1× expansion:
    # (2^2 − 1) / (2^1 − 1) = 3.0.
    assert _boundary_expansion_span_multiplier(1) == 3.0
    assert MIN_BOUNDARY_EXPANSION_HORIZON == 2  # document the assumption


def test_expand_grid_for_selected_rate_handles_non_integer_multiplier() -> None:
    from balance_pricing_engine.diagnostics import _expand_grid_for_selected_rate

    # step=5, span=80 → multiplier=2.9 gives 80*2.9=232 which is not a multiple of 5.
    # The function must snap the boundary to a valid step rather than raising ValueError.
    grid = np.arange(0.0, 0.085, 0.005)  # 0.000 to 0.080 in 0.005 steps
    expanded, did_expand = _expand_grid_for_selected_rate(grid, grid[-1], span_multiplier=2.9)
    assert did_expand
    step = float(np.diff(grid)[0])
    # Expanded grid must be step-aligned: (stop − start) / step is an integer.
    step_count = (expanded[-1] - expanded[0]) / step
    assert np.isclose(step_count, round(step_count), rtol=1e-9, atol=1e-12)
