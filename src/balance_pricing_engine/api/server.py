"""Launcher for the FastAPI serving layer. Requires the `api` optional extra.

Wraps `uvicorn.run` so TLS is an env-var-driven option (see docs/deployment.md) without
requiring a live certificate authority to test against — pass a self-signed dev cert or a
cert mounted by the deployment environment (reverse proxy, orchestrator TLS secret, etc.).
"""
from __future__ import annotations

import os
from ipaddress import ip_address
from typing import Mapping

from balance_pricing_engine.api.auth import _configured_keys
from balance_pricing_engine.api.limits import APILimits
from balance_pricing_engine.secrets.provider import StaticSecretsProvider

_CERTFILE_VAR = "PRICING_ENGINE_TLS_CERTFILE"
_KEYFILE_VAR = "PRICING_ENGINE_TLS_KEYFILE"


def resolve_tls_kwargs(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Resolve uvicorn TLS kwargs from environment variables.

    Returns {} (plain HTTP) only if neither var is set at all. Raises ValueError if only
    one is set, or if either is set but empty — a malformed TLS config (e.g. a secret
    mounted as an empty string) must fail closed rather than silently falling back to
    plain HTTP.
    """
    env = os.environ if env is None else env
    certfile = env.get(_CERTFILE_VAR)
    keyfile = env.get(_KEYFILE_VAR)
    if certfile is None and keyfile is None:
        return {}
    if certfile and keyfile:
        return {"ssl_certfile": certfile, "ssl_keyfile": keyfile}
    raise ValueError(
        f"{_CERTFILE_VAR} and {_KEYFILE_VAR} must both be set to non-empty values, or "
        "neither."
    )


def resolve_server_kwargs(env: Mapping[str, str] | None = None) -> dict[str, object]:
    """Validate serving policy before uvicorn opens a socket; never echo secret values."""
    env = os.environ if env is None else env
    APILimits.from_env(env)
    host = env.get("PRICING_ENGINE_API_HOST", "127.0.0.1")
    if not host or host != host.strip() or any(char.isspace() for char in host):
        raise ValueError("PRICING_ENGINE_API_HOST must be a non-empty bind address.")
    try:
        loopback = ip_address(host).is_loopback
    except ValueError:
        # Hostnames are not proof of a loopback binding: DNS can change.
        loopback = False
    secret = env.get("PRICING_ENGINE_API_KEYS")
    keys = _configured_keys(StaticSecretsProvider(
        {} if secret is None else {"PRICING_ENGINE_API_KEYS": secret}
    ))
    if keys == []:
        raise ValueError("PRICING_ENGINE_API_KEYS is malformed.")
    if not loopback and not keys:
        raise ValueError("Non-loopback serving requires API-key authentication.")
    values = {}
    for variable, default, maximum in (
        ("PRICING_ENGINE_API_PORT", "8000", 65535),
        ("PRICING_ENGINE_API_LIMIT_CONCURRENCY", "4", 128),
    ):
        try:
            value = int(env.get(variable, default))
        except ValueError:
            raise ValueError(f"{variable} must be a positive integer.") from None
        if not 1 <= value <= maximum:
            raise ValueError(f"{variable} must be between 1 and {maximum}.")
        values[variable] = value
    return {
        "host": host,
        "port": values["PRICING_ENGINE_API_PORT"],
        "limit_concurrency": values["PRICING_ENGINE_API_LIMIT_CONCURRENCY"],
        **resolve_tls_kwargs(env),
    }


def main() -> None:
    import uvicorn

    uvicorn.run(
        "balance_pricing_engine.api.app:app",
        **resolve_server_kwargs(),
    )


if __name__ == "__main__":
    main()
