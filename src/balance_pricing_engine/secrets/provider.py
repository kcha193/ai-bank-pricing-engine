"""Secrets-provider seam: environment-backed today, swappable for a real manager later.

A future Vault/AWS Secrets Manager/GCP Secret Manager adapter implements the same
``SecretsProvider`` protocol (``get_secret(name) -> str | None``) and can be injected
wherever a secret is read today without changing call sites. See docs/deployment.md.
"""

from __future__ import annotations

import os
from typing import Protocol


class SecretsProvider(Protocol):
    """Minimal secret-lookup interface consumers depend on."""

    def get_secret(self, name: str) -> str | None: ...


class EnvSecretsProvider:
    """Default provider: reads secrets from environment variables."""

    def get_secret(self, name: str) -> str | None:
        return os.environ.get(name)


class StaticSecretsProvider:
    """Dict-backed provider. Intended for tests and local development."""

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    def get_secret(self, name: str) -> str | None:
        return self._secrets.get(name)
