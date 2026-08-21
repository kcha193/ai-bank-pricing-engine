"""Confirms PRICING_ENGINE_API_KEYS is actually enforced on the real app's routes."""

from pathlib import Path

from fastapi.testclient import TestClient

from balance_pricing_engine.api import app
from balance_pricing_engine.api.auth import API_KEY_HEADER

CONFIG_YAML = Path("configs/base.yaml").read_text(encoding="utf-8")


def test_run_scenario_rejects_missing_key_when_configured(monkeypatch):
    monkeypatch.setenv("PRICING_ENGINE_API_KEYS", "test-key")
    client = TestClient(app)

    response = client.post("/run-scenario", json={"config_yaml": CONFIG_YAML})

    assert response.status_code == 401


def test_run_scenario_succeeds_with_correct_key_when_configured(monkeypatch):
    monkeypatch.setenv("PRICING_ENGINE_API_KEYS", "test-key")
    client = TestClient(app)

    response = client.post(
        "/run-scenario",
        json={"config_yaml": CONFIG_YAML},
        headers={API_KEY_HEADER: "test-key"},
    )

    assert response.status_code == 200


def test_v1_pricing_scenarios_rejects_missing_key_when_configured(monkeypatch):
    monkeypatch.setenv("PRICING_ENGINE_API_KEYS", "test-key")
    client = TestClient(app)

    response = client.post("/v1/pricing/scenarios", json={"config_yaml": CONFIG_YAML})

    assert response.status_code == 401


def test_v1_pricing_scenarios_succeeds_with_correct_key_when_configured(monkeypatch):
    monkeypatch.setenv("PRICING_ENGINE_API_KEYS", "test-key")
    client = TestClient(app)

    response = client.post(
        "/v1/pricing/scenarios",
        json={"config_yaml": CONFIG_YAML},
        headers={API_KEY_HEADER: "test-key"},
    )

    assert response.status_code == 200


def test_health_stays_open_when_keys_configured(monkeypatch):
    monkeypatch.setenv("PRICING_ENGINE_API_KEYS", "test-key")
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200


def test_ready_stays_open_when_keys_configured(monkeypatch):
    monkeypatch.setenv("PRICING_ENGINE_API_KEYS", "test-key")
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 200


def test_run_scenario_unaffected_when_no_keys_configured(monkeypatch):
    monkeypatch.delenv("PRICING_ENGINE_API_KEYS", raising=False)
    client = TestClient(app)

    response = client.post("/run-scenario", json={"config_yaml": CONFIG_YAML})

    assert response.status_code == 200


def test_v1_pricing_scenarios_returns_structured_error_for_malformed_keys(monkeypatch):
    """The route advertises ErrorResponse (error_code/message/correlation_id) for its
    500s -- a malformed PRICING_ENGINE_API_KEYS must fail closed with that same shape,
    not FastAPI's default {"detail": "..."} string wrapper."""
    monkeypatch.setenv("PRICING_ENGINE_API_KEYS", " , ")
    client = TestClient(app)

    response = client.post("/v1/pricing/scenarios", json={"config_yaml": CONFIG_YAML})

    assert response.status_code == 500
    body = response.json()
    assert body["error_code"] == "AUTH_CONFIG_INVALID"
    assert isinstance(body["message"], str) and body["message"]
    assert body["correlation_id"]
