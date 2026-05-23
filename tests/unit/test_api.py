import importlib
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pandas as pd

from balance_pricing_engine.api import app
from balance_pricing_engine.optimization.grid_search import GRID_RESULT_COLUMNS

api_module = importlib.import_module("balance_pricing_engine.api.app")


def test_health_returns_200_with_status_ok():
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "version" in payload


def test_run_scenario_returns_summary_for_valid_config_yaml():
    client = TestClient(app)
    config_yaml = Path("configs/base.yaml").read_text(encoding="utf-8")

    response = client.post("/run-scenario", json={"config_yaml": config_yaml})

    assert response.status_code == 200
    payload = response.json()
    expected_keys = {
        "best_loan_rate",
        "best_deposit_rate",
        "objective_value",
        "expected_loan_volume",
        "expected_deposit_volume",
        "funding_shortfall",
        "capital_charge",
        "excess_deposit_income",
        "boundary_warning_flag",
        "initial_boundary_warning_flag",
        "expansion_rounds",
        "expansion_capped",
        "policy_band_breached",
        "top_results",
    }
    assert expected_keys <= payload.keys()
    assert isinstance(payload["best_loan_rate"], float)
    assert len(payload["top_results"]) == 5
    assert set(payload["top_results"][0]) == set(GRID_RESULT_COLUMNS)


def test_run_scenario_without_sensitivity_flag_has_no_sensitivity_key():
    client = TestClient(app)
    config_yaml = Path("configs/base.yaml").read_text(encoding="utf-8")

    response = client.post("/run-scenario", json={"config_yaml": config_yaml})

    assert response.status_code == 200
    assert "sensitivity" not in response.json()


def test_run_scenario_with_include_sensitivity_false_has_no_sensitivity_key():
    client = TestClient(app)
    config_yaml = Path("configs/base.yaml").read_text(encoding="utf-8")

    response = client.post(
        "/run-scenario", json={"config_yaml": config_yaml, "include_sensitivity": False}
    )

    assert response.status_code == 200
    assert "sensitivity" not in response.json()


def test_run_scenario_with_include_sensitivity_true_returns_sensitivity_rows():
    client = TestClient(app)
    config_yaml = Path("configs/base.yaml").read_text(encoding="utf-8")

    response = client.post(
        "/run-scenario", json={"config_yaml": config_yaml, "include_sensitivity": True}
    )

    assert response.status_code == 200
    payload = response.json()
    assert "sensitivity" in payload
    rows = payload["sensitivity"]
    assert isinstance(rows, list)
    assert len(rows) > 0
    first = rows[0]
    assert "scenario_name" in first
    assert "delta_objective_pct" in first


def test_run_scenario_returns_422_for_invalid_yaml():
    client = TestClient(app)

    response = client.post("/run-scenario", json={"config_yaml": "random_seed: ["})

    assert response.status_code == 422


def test_run_scenario_returns_422_for_valid_yaml_missing_required_config_fields():
    client = TestClient(app)

    response = client.post("/run-scenario", json={"config_yaml": "random_seed: 42\n"})

    assert response.status_code == 422


def test_run_scenario_returns_422_for_unsupported_sensitivity(monkeypatch):
    def fake_run_baseline_diagnostics(config):
        best = SimpleNamespace(
            loan_rate=0.08,
            deposit_rate=0.05,
            total_objective=1_000_000.0,
            expected_loan_volume=100_000_000.0,
            expected_deposit_volume=80_000_000.0,
            funding_shortfall=0.0,
            capital_charge=20_000.0,
            excess_deposit_income=0.0,
        )
        top_results = pd.DataFrame([{column: 0.0 for column in GRID_RESULT_COLUMNS}])
        return SimpleNamespace(
            best_result=best,
            boundary_diagnostics=SimpleNamespace(has_warning=False),
            initial_boundary_diagnostics=SimpleNamespace(has_warning=False),
            expansion_rounds=0,
            expansion_capped=False,
            policy_band_breached=False,
            top_results=lambda count: top_results,
        )

    def fake_run_sensitivity_analysis(config):
        raise NotImplementedError(
            "Sensitivity analysis is not supported for segmented pricing configurations."
        )

    monkeypatch.setattr(api_module, "run_baseline_diagnostics", fake_run_baseline_diagnostics)
    monkeypatch.setattr(api_module, "run_sensitivity_analysis", fake_run_sensitivity_analysis)
    client = TestClient(app)
    config_yaml = Path("configs/segmented_pricing.yaml").read_text(encoding="utf-8")

    response = client.post(
        "/run-scenario", json={"config_yaml": config_yaml, "include_sensitivity": True}
    )

    assert response.status_code == 422
    assert "not supported for segmented pricing" in response.json()["detail"]
