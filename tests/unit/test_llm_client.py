import sys
from types import SimpleNamespace

import pytest

from balance_pricing_engine.llm._client import build_client, is_llm_available, is_sdk_installed


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


def test_build_client_raises_import_error_when_sdk_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)

    with pytest.raises(ImportError, match="anthropic"):
        build_client()
