"""Lazy Anthropic SDK loader - importable without the SDK installed."""
from __future__ import annotations

from typing import Any

from balance_pricing_engine.secrets.provider import EnvSecretsProvider, SecretsProvider

_API_KEY_NAME = "ANTHROPIC_API_KEY"


def build_client(
    api_key: str | None = None,
    secrets_provider: SecretsProvider | None = None,
) -> Any:
    """Return an Anthropic client, or raise ImportError with install hint.

    Resolves the API key from `api_key` if given, else from `secrets_provider`
    (default: environment variables, preserving prior behaviour). A future
    Vault/AWS Secrets Manager/GCP Secret Manager provider can be injected here
    without changing callers.

    An explicitly injected `secrets_provider` is authoritative: if it has no key, this
    raises rather than constructing a bare `anthropic.Anthropic()`, which would let the
    SDK silently rediscover an ambient `ANTHROPIC_API_KEY` from the process environment
    -- bypassing the caller's selected provider and disagreeing with
    `is_llm_available(secrets_provider)`, which returns False for the same
    configuration. No `secrets_provider` at all still defaults to `EnvSecretsProvider`,
    preserving prior behaviour (including its own fall-through to the SDK's env lookup).
    """
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError(
            "The 'anthropic' package is required for LLM features. "
            "Install it with: pip install 'balance_pricing_engine[llm]'"
        ) from exc
    if api_key:
        return anthropic.Anthropic(api_key=api_key)
    # `is None`, not `or`: a falsey-but-real provider (e.g. one whose __bool__/__len__
    # reflects "currently holds no secrets") must still be used and still be authoritative
    # -- `or` would silently discard it in favour of EnvSecretsProvider, letting the
    # ambient environment leak through exactly the case the raise below exists to prevent.
    provider = EnvSecretsProvider() if secrets_provider is None else secrets_provider
    resolved_key = provider.get_secret(_API_KEY_NAME)
    if resolved_key:
        return anthropic.Anthropic(api_key=resolved_key)
    if secrets_provider is not None:
        raise ValueError(
            f"No {_API_KEY_NAME} available from the injected secrets provider; refusing "
            "to fall back to the Anthropic SDK's own environment lookup."
        )
    return anthropic.Anthropic()


def is_sdk_installed() -> bool:
    """Return True if the anthropic SDK package is importable."""
    try:
        import anthropic  # noqa: F401

        return True
    except ImportError:
        return False


def is_llm_available(secrets_provider: SecretsProvider | None = None) -> bool:
    """Return True if the anthropic SDK is installed and an API key is resolvable."""
    try:
        import anthropic  # noqa: F401

        provider = EnvSecretsProvider() if secrets_provider is None else secrets_provider
        resolved_key = provider.get_secret(_API_KEY_NAME)
        return bool(resolved_key)
    except ImportError:
        return False
