"""Lazy Anthropic SDK loader - importable without the SDK installed."""
from __future__ import annotations

import os
from typing import Any


def build_client(api_key: str | None = None) -> Any:
    """Return an Anthropic client, or raise ImportError with install hint."""
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError(
            "The 'anthropic' package is required for LLM features. "
            "Install it with: pip install 'balance_pricing_engine[llm]'"
        ) from exc
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def is_sdk_installed() -> bool:
    """Return True if the anthropic SDK package is importable."""
    try:
        import anthropic  # noqa: F401

        return True
    except ImportError:
        return False


def is_llm_available() -> bool:
    """Return True if the anthropic SDK is installed and ANTHROPIC_API_KEY is set."""
    try:
        import anthropic  # noqa: F401

        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    except ImportError:
        return False
