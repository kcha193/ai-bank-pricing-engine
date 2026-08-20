import sys
from types import SimpleNamespace

import pytest

from balance_pricing_engine.llm._client import build_client, is_llm_available, is_sdk_installed
from balance_pricing_engine.secrets.provider import StaticSecretsProvider


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
