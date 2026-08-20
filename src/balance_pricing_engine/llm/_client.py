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
    """
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError(
            "The 'anthropic' package is required for LLM features. "
            "Install it with: pip install 'balance_pricing_engine[llm]'"
        ) from exc
    resolved_key = api_key or (secrets_provider or EnvSecretsProvider()).get_secret(_API_KEY_NAME)
    return anthropic.Anthropic(api_key=resolved_key) if resolved_key else anthropic.Anthropic()


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

        resolved_key = (secrets_provider or EnvSecretsProvider()).get_secret(_API_KEY_NAME)
        return bool(resolved_key)
    except ImportError:
        return False
