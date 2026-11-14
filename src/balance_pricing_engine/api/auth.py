"""API-key auth for compute-triggering routes.

Optional for loopback serving — set PRICING_ENGINE_API_KEYS
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
    # Spaces around comma-separated entries support existing rotation configuration.
    # Empty entries, control characters and non-ASCII keys are configuration errors,
    # not entries to silently discard. compare_digest(str, str) requires ASCII.
    if any(character in raw for character in "\r\n\t"):
        return []
    keys = [key.strip(" ") for key in raw.split(",")]
    if any(not key or any(not 33 <= ord(char) <= 126 for char in key) for key in keys):
        return []
    return keys


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
                "message": "PRICING_ENGINE_API_KEYS is malformed.",
                "correlation_id": correlation_id,
            },
        )
    presented = request.headers.get(API_KEY_HEADER)
    if presented is None or not any(
        hmac.compare_digest(presented.encode("utf-8"), key.encode("ascii"))
        for key in configured
    ):
        # This dependency runs before the route body is parsed, so there's no request-body
        # correlation_id to consult -- only the header-or-generate half of the documented
        # resolution order (see PricingScenarioRequestV1.correlation_id). Set directly via
        # HTTPException.headers rather than a structured dict detail: _structured_http_
        # exception_handler merges exc.headers into the response regardless of detail
        # shape, so this preserves the plain-string 401 body both routes already return.
        correlation_id = request.headers.get(_CORRELATION_ID_HEADER) or str(uuid.uuid4())
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API key.",
            headers={_CORRELATION_ID_HEADER: correlation_id},
        )
