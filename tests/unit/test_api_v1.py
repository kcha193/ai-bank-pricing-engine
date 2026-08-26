"""Tests for the versioned, governed /v1/pricing/scenarios endpoint and /ready."""

import importlib
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from balance_pricing_engine.api import app
from balance_pricing_engine.api.schemas import _EXAMPLE_CONFIG_YAML
from balance_pricing_engine.application.pricing_service import PricingService
from balance_pricing_engine.config import EngineConfig
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


def test_example_config_yaml_matches_recalibrated_defaults():
    """The OpenAPI example for config_yaml must not silently mix pre- and
    post-recalibration values.

    _EXAMPLE_CONFIG_YAML omits response.coefficients, so a caller who copies
    it verbatim inherits whatever LoanResponseCoefficientsConfig defaults to
    (rate_anchor 0.0499, the post-2026-08-26-recalibration market anchor --
    see docs/modelling_assumptions.md). Its explicitly-set market rate and
    elasticities must track that same calibration, or a copy-pasted request
    silently prices against an unintended hybrid of old and new assumptions.
    """
    example_config = EngineConfig.model_validate(yaml.safe_load(_EXAMPLE_CONFIG_YAML))
    canonical_config = EngineConfig.model_validate(yaml.safe_load(CONFIG_YAML))

    # rate_anchor is the reference price at which the loan response's
    # own-rate term vanishes -- it must track the market rate being priced
    # into, or the model treats the wrong rate as "neutral".
    assert (
        example_config.response.coefficients.loan.rate_anchor
        == example_config.market.competitor_loan_rate
        == example_config.market.home_loan_reference_rate
    )
    assert (
        example_config.response.loan_rate_elasticity
        == canonical_config.response.loan_rate_elasticity
    )
    assert (
        example_config.response.loan_competitor_gap_elasticity
        == canonical_config.response.loan_competitor_gap_elasticity
    )


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
    # Regression: the correlation-ID header set on the injected Response never reaches
    # an error path (a fresh JSONResponse is built by the exception handler instead),
    # so it must be copied from the structured detail onto the response headers.
    assert response.headers["X-Correlation-ID"] == body["correlation_id"]


def test_v1_pricing_scenarios_returns_422_for_missing_required_fields():
    client = TestClient(app)

    response = client.post("/v1/pricing/scenarios", json={"config_yaml": "random_seed: 42\n"})

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


def test_v1_pricing_scenarios_returns_structured_422_for_request_schema_violation():
    """A body that fails `PricingScenarioRequestV1` validation itself (missing the
    required `config_yaml` field) raises `RequestValidationError` before
    `create_pricing_scenario` ever runs, bypassing `_error_detail` entirely. The route
    still advertises `ErrorResponse` for 422, so this pre-handler path must be shaped
    the same way as every other validation failure, with a correlation ID header."""
    client = TestClient(app)

    response = client.post("/v1/pricing/scenarios", json={})

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    assert set(body) == {"error_code", "message", "correlation_id"}
    assert response.headers["X-Correlation-ID"] == body["correlation_id"]


def test_run_scenario_request_schema_violation_keeps_default_shape():
    """The `RequestValidationError` handler is scoped to `/v1/pricing/scenarios` --
    the legacy /run-scenario endpoint doesn't advertise `ErrorResponse` or correlation
    IDs, so its pre-handler validation failures must keep FastAPI's default
    `{"detail": [...]}` shape unchanged."""
    client = TestClient(app)

    response = client.post("/run-scenario", json={})

    assert response.status_code == 422
    body = response.json()
    assert "detail" in body
    assert isinstance(body["detail"], list)


def test_v1_pricing_scenarios_returns_503_for_governance_error(monkeypatch):
    def _raise_governance_error(self, request):
        raise GovernanceError("Approved champion artifact missing.")

    monkeypatch.setattr(PricingService, "run_scenario", _raise_governance_error)
    client = TestClient(app)

    response = client.post("/v1/pricing/scenarios", json={"config_yaml": CONFIG_YAML})

    assert response.status_code == 503
    body = response.json()
    assert body["error_code"] == "MODEL_ARTIFACT_UNAVAILABLE"
    assert response.headers["X-Correlation-ID"] == body["correlation_id"]


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


def test_v1_pricing_scenarios_returns_sensitivity_when_requested():
    # Regression: include_sensitivity=true made PricingService run the analysis, but
    # _decision_to_response() dropped decision.sensitivity and the response schema had
    # no field for it, so callers paid for the extra analysis and got an
    # indistinguishable response from include_sensitivity=false.
    client = TestClient(app)

    response = client.post(
        "/v1/pricing/scenarios",
        json={"config_yaml": CONFIG_YAML, "include_sensitivity": True},
    )

    assert response.status_code == 200
    sensitivity = response.json()["sensitivity"]
    assert isinstance(sensitivity, list)
    assert len(sensitivity) > 0
    assert "delta_objective_pct" in sensitivity[0]
    assert "parameter_family" in sensitivity[0]


def test_v1_pricing_scenarios_omits_sensitivity_when_not_requested():
    client = TestClient(app)

    response = client.post(
        "/v1/pricing/scenarios",
        json={"config_yaml": CONFIG_YAML, "include_sensitivity": False},
    )

    assert response.status_code == 200
    assert response.json()["sensitivity"] is None


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
