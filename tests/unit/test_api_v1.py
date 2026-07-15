"""Tests for the versioned, governed /v1/pricing/scenarios endpoint and /ready."""

import importlib
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from balance_pricing_engine.api import app
from balance_pricing_engine.application.pricing_service import PricingService
from balance_pricing_engine.optimization.response_fn import GovernanceError

api_module = importlib.import_module("balance_pricing_engine.api.app")

CONFIG_YAML = Path("configs/base.yaml").read_text(encoding="utf-8")


def test_ready_returns_200_for_default_config():
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_ready_returns_503_when_config_path_env_points_at_missing_file(monkeypatch):
    monkeypatch.setenv("PRICING_ENGINE_CONFIG_PATH", "configs/does_not_exist.yaml")
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["error_code"] == "CONFIG_UNAVAILABLE"


def test_v1_pricing_scenarios_returns_governed_decision():
    client = TestClient(app)

    response = client.post(
        "/v1/pricing/scenarios",
        json={"config_yaml": CONFIG_YAML, "scenario_name": "api_v1_test"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scenario_name"] == "api_v1_test"
    assert payload["status"] in {"RECOMMENDED", "REVIEW_REQUIRED", "BLOCKED"}
    assert isinstance(payload["best_loan_rate"], float)
    assert len(payload["governance_gates"]) > 0
    assert set(payload["model_lineage"]) == {"loan", "deposit"}
    assert set(payload["feature_lineage"]) == {"loan", "deposit"}
    assert "X-Correlation-ID" in response.headers
    assert payload["correlation_id"] == response.headers["X-Correlation-ID"]


def test_v1_pricing_scenarios_echoes_caller_supplied_correlation_id():
    client = TestClient(app)

    response = client.post(
        "/v1/pricing/scenarios",
        json={"config_yaml": CONFIG_YAML, "correlation_id": "caller-supplied-id"},
    )

    assert response.status_code == 200
    assert response.json()["correlation_id"] == "caller-supplied-id"
    assert response.headers["X-Correlation-ID"] == "caller-supplied-id"


def test_v1_pricing_scenarios_honours_correlation_id_header():
    client = TestClient(app)

    response = client.post(
        "/v1/pricing/scenarios",
        json={"config_yaml": CONFIG_YAML},
        headers={"X-Correlation-ID": "header-supplied-id"},
    )

    assert response.status_code == 200
    assert response.json()["correlation_id"] == "header-supplied-id"


def test_v1_pricing_scenarios_returns_422_for_invalid_yaml():
    client = TestClient(app)

    response = client.post("/v1/pricing/scenarios", json={"config_yaml": "random_seed: ["})

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    # The route advertises `ErrorResponse` (error_code/message/correlation_id) as the
    # top-level 422/503/500 schema -- it must not be nested under a "detail" wrapper.
    assert set(body) == {"error_code", "message", "correlation_id"}


def test_v1_pricing_scenarios_returns_422_for_missing_required_fields():
    client = TestClient(app)

    response = client.post("/v1/pricing/scenarios", json={"config_yaml": "random_seed: 42\n"})

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


def test_v1_pricing_scenarios_returns_503_for_governance_error(monkeypatch):
    def _raise_governance_error(self, request):
        raise GovernanceError("Approved champion artifact missing.")

    monkeypatch.setattr(PricingService, "run_scenario", _raise_governance_error)
    client = TestClient(app)

    response = client.post("/v1/pricing/scenarios", json={"config_yaml": CONFIG_YAML})

    assert response.status_code == 503
    assert response.json()["error_code"] == "MODEL_ARTIFACT_UNAVAILABLE"


def test_v1_pricing_scenarios_returns_422_for_missing_strict_competitor_rate_source():
    """A missing/failed strict competitor-rate source is caller input, not a server
    outage: PricingService.run_scenario raises before optimisation (from
    apply_competitor_rates_to_config), and this must surface as 422, not 500."""
    raw_config = yaml.safe_load(CONFIG_YAML)
    raw_config["competitor_rates"] = {
        **raw_config["competitor_rates"],
        "enabled": True,
        "strict": True,
        "source_file": "data/processed/competitor_rates/does_not_exist.json",
    }
    client = TestClient(app)

    response = client.post(
        "/v1/pricing/scenarios", json={"config_yaml": yaml.safe_dump(raw_config)}
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "COMPETITOR_RATE_VALIDATION_ERROR"


def test_v1_pricing_scenarios_does_not_mislabel_unrelated_value_error_as_competitor_rate(
    monkeypatch,
):
    """A ValueError from an unrelated part of run_scenario (e.g. a segmented-optimizer
    config error, not competitor-rate resolution) must not be mislabeled as
    COMPETITOR_RATE_VALIDATION_ERROR -- that except clause is scoped to
    CompetitorRateResolutionError specifically, so this falls through to 500."""

    def _raise_unrelated_value_error(self, request):
        raise ValueError("Tier 'high_balance' has no customers.")

    monkeypatch.setattr(PricingService, "run_scenario", _raise_unrelated_value_error)
    client = TestClient(app)

    response = client.post("/v1/pricing/scenarios", json={"config_yaml": CONFIG_YAML})

    assert response.status_code == 500
    assert response.json()["error_code"] == "INTERNAL_ERROR"


def test_v1_pricing_scenarios_returns_500_for_unexpected_error_without_stack_trace(monkeypatch):
    def _raise_unexpected(self, request):
        raise RuntimeError("boom: internal secret detail")

    monkeypatch.setattr(PricingService, "run_scenario", _raise_unexpected)
    client = TestClient(app)

    response = client.post("/v1/pricing/scenarios", json={"config_yaml": CONFIG_YAML})

    assert response.status_code == 500
    body = response.json()
    assert body["error_code"] == "INTERNAL_ERROR"
    assert "boom" not in body["message"]


def test_v1_pricing_scenarios_can_persist_ledger_and_include_decision_record(tmp_path, monkeypatch):
    # persist_ledger writes to the default relative ledger_base_dir; chdir isolates
    # the resulting data/outputs/runs/ directory inside tmp_path for this test.
    monkeypatch.chdir(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/v1/pricing/scenarios",
        json={
            "config_yaml": CONFIG_YAML,
            "scenario_name": "api_ledger_test",
            "persist_ledger": True,
        },
    )

    assert response.status_code == 200
    run_dir = Path(response.json()["run_dir"])
    assert (run_dir / "decision_record.json").exists()


def test_run_scenario_backward_compatible_response_unchanged():
    """Regression: the original endpoint's response shape must not change."""
    client = TestClient(app)

    response = client.post("/run-scenario", json={"config_yaml": CONFIG_YAML})

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
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
