from pathlib import Path

from fastapi.testclient import TestClient

from balance_pricing_engine.api import app
from balance_pricing_engine.optimization.grid_search import GRID_RESULT_COLUMNS


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


def test_run_scenario_returns_422_for_invalid_yaml():
    client = TestClient(app)

    response = client.post("/run-scenario", json={"config_yaml": "random_seed: ["})

    assert response.status_code == 422


def test_run_scenario_returns_422_for_valid_yaml_missing_required_config_fields():
    client = TestClient(app)

    response = client.post("/run-scenario", json={"config_yaml": "random_seed: 42\n"})

    assert response.status_code == 422
