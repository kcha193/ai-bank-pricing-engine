"""Launcher for the FastAPI serving layer. Requires the `api` optional extra.

Wraps `uvicorn.run` so TLS is an env-var-driven option (see docs/deployment.md) without
requiring a live certificate authority to test against — pass a self-signed dev cert or a
cert mounted by the deployment environment (reverse proxy, orchestrator TLS secret, etc.).
"""
from __future__ import annotations

import os
from typing import Mapping

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
        f"neither — got certfile={certfile!r}, keyfile={keyfile!r}."
    )


def main() -> None:
    import uvicorn

    host = os.environ.get("PRICING_ENGINE_API_HOST", "0.0.0.0")
    port = int(os.environ.get("PRICING_ENGINE_API_PORT", "8000"))
    uvicorn.run(
        "balance_pricing_engine.api.app:app",
        host=host,
        port=port,
        **resolve_tls_kwargs(),
    )


if __name__ == "__main__":
    main()
