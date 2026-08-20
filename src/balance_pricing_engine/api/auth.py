"""Opt-in API-key auth for compute-triggering routes.

Disabled by default (today's behaviour is unchanged) — set PRICING_ENGINE_API_KEYS
(comma-separated for basic key rotation) to enable. This is a defense-in-depth layer for
deployments with no fronting proxy; the recommended production default is still an API
gateway, mTLS, or auth proxy in front of the container — see docs/deployment.md.
"""
from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request

from balance_pricing_engine.secrets.provider import EnvSecretsProvider, SecretsProvider

API_KEY_HEADER = "X-API-Key"
_API_KEYS_SECRET_NAME = "PRICING_ENGINE_API_KEYS"


def _default_secrets_provider() -> SecretsProvider:
    return EnvSecretsProvider()


def _configured_keys(secrets_provider: SecretsProvider) -> list[str]:
    raw = secrets_provider.get_secret(_API_KEYS_SECRET_NAME)
    if not raw:
        return []
    return [key.strip() for key in raw.split(",") if key.strip()]


def require_api_key(
    request: Request,
    secrets_provider: SecretsProvider = Depends(_default_secrets_provider),
) -> None:
    """FastAPI dependency: enforce X-API-Key only when PRICING_ENGINE_API_KEYS is set."""
    configured = _configured_keys(secrets_provider)
    if not configured:
        return
    presented = request.headers.get(API_KEY_HEADER)
    if presented is None or not any(hmac.compare_digest(presented, key) for key in configured):
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")
