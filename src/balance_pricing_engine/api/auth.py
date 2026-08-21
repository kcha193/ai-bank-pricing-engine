"""Opt-in API-key auth for compute-triggering routes.

Disabled by default (today's behaviour is unchanged) — set PRICING_ENGINE_API_KEYS
(comma-separated for basic key rotation) to enable. This is a defense-in-depth layer for
deployments with no fronting proxy; the recommended production default is still an API
gateway, mTLS, or auth proxy in front of the container — see docs/deployment.md.
"""
from __future__ import annotations

import hmac
import uuid

from fastapi import Depends, HTTPException, Request

from balance_pricing_engine.secrets.provider import EnvSecretsProvider, SecretsProvider

API_KEY_HEADER = "X-API-Key"
_API_KEYS_SECRET_NAME = "PRICING_ENGINE_API_KEYS"
_CORRELATION_ID_HEADER = "X-Correlation-ID"


def _default_secrets_provider() -> SecretsProvider:
    return EnvSecretsProvider()


def _configured_keys(secrets_provider: SecretsProvider) -> list[str] | None:
    """Return configured keys, `[]` if set but malformed, or `None` if unset entirely."""
    raw = secrets_provider.get_secret(_API_KEYS_SECRET_NAME)
    if raw is None:
        return None
    return [key.strip() for key in raw.split(",") if key.strip()]


def require_api_key(
    request: Request,
    secrets_provider: SecretsProvider = Depends(_default_secrets_provider),
) -> None:
    """FastAPI dependency: enforce X-API-Key only when PRICING_ENGINE_API_KEYS is set.

    A configured-but-invalid secret (empty, or only whitespace/separators) fails closed
    with a 500 rather than silently disabling auth like an unset secret would. The detail
    is a dict matching ``api.schemas.ErrorResponse`` (error_code/message/correlation_id)
    rather than a plain string: ``/v1/pricing/scenarios`` advertises that schema for its
    500s, and ``api.app``'s HTTPException handler serializes a dict detail as the
    response body verbatim, so a plain string here would silently break that contract.
    """
    configured = _configured_keys(secrets_provider)
    if configured is None:
        return
    if not configured:
        correlation_id = request.headers.get(_CORRELATION_ID_HEADER) or str(uuid.uuid4())
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "AUTH_CONFIG_INVALID",
                "message": "PRICING_ENGINE_API_KEYS is set but contains no valid keys.",
                "correlation_id": correlation_id,
            },
        )
    presented = request.headers.get(API_KEY_HEADER)
    if presented is None or not any(hmac.compare_digest(presented, key) for key in configured):
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")
