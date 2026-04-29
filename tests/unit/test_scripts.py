import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


def load_script(path: str):
    script_path = Path(path)
    spec = importlib.util.spec_from_file_location(script_path.stem, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_baseline_workflow_script_uses_package_workflow_helpers(monkeypatch, capsys):
    script = load_script("scripts/run_baseline_workflow.py")
    best_result = SimpleNamespace(
        loan_rate=0.06,
        deposit_rate=0.04,
        objective_value=1_000.0,
        loan_income_after_losses=900.0,
        deposit_cost=100.0,
        wholesale_funding_cost=50.0,
        funding_penalty=0.0,
        expected_loan_volume=10_000.0,
        expected_deposit_volume=6_500.0,
        funding_shortfall=0.0,
        home_loan_spread_to_market_bps=75.0,
        term_deposit_spread_to_market_bps=0.0,
    )
    diagnostics = SimpleNamespace(
        best_result=best_result,
        top_results=lambda n: pd.DataFrame(
            {
                "loan_rate": [0.06],
                "deposit_rate": [0.04],
                "total_objective": [1_000.0],
                "expected_loan_volume": [10_000.0],
                "expected_deposit_volume": [6_500.0],
                "funding_shortfall": [0.0],
            }
        ),
        winner_decomposition=lambda: SimpleNamespace(
            loan_interest_income=1_200.0,
            expected_credit_loss=100.0,
            deposit_interest_cost=50.0,
            wholesale_funding_cost=50.0,
            funding_shortfall_penalty=0.0,
            total_objective=1_000.0,
        ),
        boundary_diagnostics=SimpleNamespace(has_warning=False, warnings=()),
    )
    config = SimpleNamespace(
        market=SimpleNamespace(home_loan_reference_rate=0.0525, term_deposit_reference_rate=0.04)
    )

    monkeypatch.setattr(script, "load_config", lambda path: config)
    monkeypatch.setattr(script, "run_baseline_diagnostics", lambda loaded_config: diagnostics)
    monkeypatch.setattr(
        script,
        "run_sensitivity_analysis",
        lambda loaded_config: pd.DataFrame(
            {
                "scenario_name": ["baseline"],
                "best_loan_rate": [0.06],
                "best_deposit_rate": [0.04],
                "objective_value": [1_000.0],
                "expected_loan_volume": [10_000.0],
                "expected_deposit_volume": [6_500.0],
                "funding_shortfall": [0.0],
                "boundary_warning_flag": [False],
            }
        ),
    )

    script.main()

    output = capsys.readouterr().out
    assert "Best joint pricing decision" in output
    assert "Market-Relative Pricing Recommendation" in output
    assert "Sensitivity summary" in output


def test_response_model_workflow_script_prints_report_metrics(monkeypatch, capsys):
    script = load_script("scripts/train_response_models_workflow.py")
    workflow_result = SimpleNamespace(
        loan_model_report={
            "title": "Home loan acceptance model",
            "metrics": {"auc": 0.9},
            "calibration": pd.DataFrame(
                {
                    "bin": [0],
                    "mean_predicted_probability": [0.8],
                    "observed_rate": [0.75],
                    "count": [10],
                }
            ),
            "response_check": SimpleNamespace(
                is_directionally_sane=True,
                expected_direction="decreasing",
                violations=0,
                rate_response=pd.DataFrame(
                    {"offered_rate": [0.05], "mean_predicted_probability": [0.8]}
                ),
            ),
        },
        deposit_model_report={
            "title": "Term deposit uptake model",
            "metrics": {"auc": 0.8},
            "calibration": pd.DataFrame(
                {
                    "bin": [0],
                    "mean_predicted_probability": [0.4],
                    "observed_rate": [0.45],
                    "count": [12],
                }
            ),
            "response_check": SimpleNamespace(
                is_directionally_sane=True,
                expected_direction="increasing",
                violations=0,
                rate_response=pd.DataFrame(
                    {"offered_rate": [0.04], "mean_predicted_probability": [0.4]}
                ),
            ),
        },
    )

    monkeypatch.setattr(script, "load_config", lambda path: "config")
    monkeypatch.setattr(script, "run_response_model_workflow", lambda config: workflow_result)

    script.main()

    output = capsys.readouterr().out
    assert "Home loan acceptance model" in output
    assert "Term deposit uptake model" in output
    assert "auc: 0.9000" in output
    assert "Rate response sanity check: pass" in output
