"""Tests for the opt-in API-key auth dependency."""

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from balance_pricing_engine.api.auth import (
    API_KEY_HEADER,
    _default_secrets_provider,
    require_api_key,
)
from balance_pricing_engine.secrets.provider import StaticSecretsProvider


def _protected_app(secrets: dict[str, str]) -> FastAPI:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    def protected() -> dict[str, str]:
        return {"status": "ok"}

    app.dependency_overrides[_default_secrets_provider] = lambda: StaticSecretsProvider(secrets)
    return app


def test_require_api_key_allows_request_when_no_keys_configured():
    client = TestClient(_protected_app({}))

    response = client.get("/protected")

    assert response.status_code == 200


def test_require_api_key_rejects_missing_header_when_keys_configured():
    client = TestClient(_protected_app({"PRICING_ENGINE_API_KEYS": "valid-key"}))

    response = client.get("/protected")

    assert response.status_code == 401


def test_require_api_key_rejects_wrong_key():
    client = TestClient(_protected_app({"PRICING_ENGINE_API_KEYS": "valid-key"}))

    response = client.get("/protected", headers={API_KEY_HEADER: "wrong-key"})

    assert response.status_code == 401


def test_require_api_key_accepts_correct_key():
    client = TestClient(_protected_app({"PRICING_ENGINE_API_KEYS": "valid-key"}))

    response = client.get("/protected", headers={API_KEY_HEADER: "valid-key"})

    assert response.status_code == 200


def test_require_api_key_accepts_any_configured_key_when_multiple_keys_set():
    client = TestClient(_protected_app({"PRICING_ENGINE_API_KEYS": "key-one, key-two"}))

    response = client.get("/protected", headers={API_KEY_HEADER: "key-two"})

    assert response.status_code == 200
