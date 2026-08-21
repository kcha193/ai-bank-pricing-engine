import pytest

from balance_pricing_engine.api.server import resolve_tls_kwargs


def test_resolve_tls_kwargs_returns_empty_dict_when_neither_var_set():
    assert resolve_tls_kwargs({}) == {}


def test_resolve_tls_kwargs_returns_ssl_kwargs_when_both_vars_set():
    env = {
        "PRICING_ENGINE_TLS_CERTFILE": "/certs/server.crt",
        "PRICING_ENGINE_TLS_KEYFILE": "/certs/server.key",
    }
    assert resolve_tls_kwargs(env) == {
        "ssl_certfile": "/certs/server.crt",
        "ssl_keyfile": "/certs/server.key",
    }


def test_resolve_tls_kwargs_raises_when_only_certfile_set():
    env = {"PRICING_ENGINE_TLS_CERTFILE": "/certs/server.crt"}
    with pytest.raises(ValueError, match="both.*or neither"):
        resolve_tls_kwargs(env)


def test_resolve_tls_kwargs_raises_when_only_keyfile_set():
    env = {"PRICING_ENGINE_TLS_KEYFILE": "/certs/server.key"}
    with pytest.raises(ValueError, match="both.*or neither"):
        resolve_tls_kwargs(env)


def test_resolve_tls_kwargs_raises_when_both_vars_set_but_empty():
    """A secret mounted as an empty string must fail closed, not silently start plain HTTP."""
    env = {"PRICING_ENGINE_TLS_CERTFILE": "", "PRICING_ENGINE_TLS_KEYFILE": ""}
    with pytest.raises(ValueError, match="both.*or neither"):
        resolve_tls_kwargs(env)


def test_resolve_tls_kwargs_raises_when_certfile_set_but_empty():
    env = {"PRICING_ENGINE_TLS_CERTFILE": "", "PRICING_ENGINE_TLS_KEYFILE": "/certs/server.key"}
    with pytest.raises(ValueError, match="both.*or neither"):
        resolve_tls_kwargs(env)


def test_resolve_tls_kwargs_defaults_to_os_environ(monkeypatch):
    monkeypatch.delenv("PRICING_ENGINE_TLS_CERTFILE", raising=False)
    monkeypatch.delenv("PRICING_ENGINE_TLS_KEYFILE", raising=False)
    assert resolve_tls_kwargs() == {}
