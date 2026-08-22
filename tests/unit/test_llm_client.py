import sys
from types import SimpleNamespace

import pytest

from balance_pricing_engine.llm._client import build_client, is_llm_available, is_sdk_installed
from balance_pricing_engine.secrets.provider import StaticSecretsProvider


class _FalseyStaticSecretsProvider(StaticSecretsProvider):
    """A provider whose truthiness reflects 'currently holds no secrets' via __len__ --
    distinct from being None. Plain StaticSecretsProvider has no __bool__/__len__ and is
    always truthy, so it can't exercise the `secrets_provider or EnvSecretsProvider()`
    bug: an `or` check discards *this* provider even when explicitly injected, silently
    substituting EnvSecretsProvider instead."""

    def __len__(self) -> int:
        return len(self._secrets)


def test_is_sdk_installed_returns_bool():
    assert isinstance(is_sdk_installed(), bool)


def test_is_llm_available_false_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert is_llm_available() is False


def test_is_llm_available_true_with_sdk_and_key(monkeypatch):
    fake_anthropic = SimpleNamespace(Anthropic=lambda: object())
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    assert is_llm_available() is True


def test_is_llm_available_true_with_key_from_injected_secrets_provider(monkeypatch):
    fake_anthropic = SimpleNamespace(Anthropic=lambda: object())
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = StaticSecretsProvider({"ANTHROPIC_API_KEY": "test-key"})

    assert is_llm_available(secrets_provider=provider) is True


def test_build_client_raises_import_error_when_sdk_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)

    with pytest.raises(ImportError, match="anthropic"):
        build_client()


def test_build_client_uses_key_from_injected_secrets_provider(monkeypatch):
    captured = {}

    class FakeAnthropic:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key

    fake_anthropic = SimpleNamespace(Anthropic=FakeAnthropic)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    provider = StaticSecretsProvider({"ANTHROPIC_API_KEY": "from-provider"})

    build_client(secrets_provider=provider)

    assert captured["api_key"] == "from-provider"


def test_build_client_explicit_api_key_overrides_secrets_provider(monkeypatch):
    captured = {}

    class FakeAnthropic:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key

    fake_anthropic = SimpleNamespace(Anthropic=FakeAnthropic)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    provider = StaticSecretsProvider({"ANTHROPIC_API_KEY": "from-provider"})

    build_client(api_key="explicit-key", secrets_provider=provider)

    assert captured["api_key"] == "explicit-key"


def test_build_client_does_not_leak_ambient_env_key_around_an_injected_provider(monkeypatch):
    """An injected secrets_provider with no key must be authoritative -- it must not be
    silently bypassed by the Anthropic SDK's own ANTHROPIC_API_KEY environment lookup,
    which would disagree with is_llm_available(secrets_provider) for the same config."""
    captured = {}

    class FakeAnthropic:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key

    fake_anthropic = SimpleNamespace(Anthropic=FakeAnthropic)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-env-key")
    provider = StaticSecretsProvider({})

    assert is_llm_available(secrets_provider=provider) is False
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        build_client(secrets_provider=provider)
    assert "api_key" not in captured


def test_is_llm_available_false_for_falsey_but_explicitly_injected_provider(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda: object()))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-env-key")
    provider = _FalseyStaticSecretsProvider({})

    assert is_llm_available(secrets_provider=provider) is False


def test_build_client_does_not_leak_ambient_key_for_falsey_provider(monkeypatch):
    captured = {}

    class FakeAnthropic:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-env-key")
    provider = _FalseyStaticSecretsProvider({})

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        build_client(secrets_provider=provider)
    assert "api_key" not in captured


def test_build_client_falls_back_to_env_when_no_provider_injected(monkeypatch):
    """No secrets_provider at all preserves prior behaviour: fall through to the SDK's
    own ANTHROPIC_API_KEY environment lookup (EnvSecretsProvider reads the same var, so
    this only matters if the SDK's own resolution ever diverges from a plain env read)."""
    captured = {}

    class FakeAnthropic:
        def __init__(self, api_key=None):
            captured["called"] = True

    fake_anthropic = SimpleNamespace(Anthropic=FakeAnthropic)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    build_client()  # must not raise

    assert captured["called"] is True
