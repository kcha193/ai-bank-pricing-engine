"""Tests for the secrets-provider seam."""

from balance_pricing_engine.secrets.provider import EnvSecretsProvider, StaticSecretsProvider


def test_env_secrets_provider_reads_from_environment(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "shh")
    provider = EnvSecretsProvider()

    assert provider.get_secret("MY_SECRET") == "shh"


def test_env_secrets_provider_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("MISSING_SECRET", raising=False)
    provider = EnvSecretsProvider()

    assert provider.get_secret("MISSING_SECRET") is None


def test_static_secrets_provider_returns_configured_value():
    provider = StaticSecretsProvider({"MY_SECRET": "shh"})

    assert provider.get_secret("MY_SECRET") == "shh"


def test_static_secrets_provider_returns_none_for_unknown_key():
    provider = StaticSecretsProvider({})

    assert provider.get_secret("MISSING_SECRET") is None
