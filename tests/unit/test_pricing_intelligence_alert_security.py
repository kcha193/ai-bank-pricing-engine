"""Alert failures never disclose destination credentials or untrusted text."""

import logging
from http.client import InvalidURL
import threading

import pytest

from balance_pricing_engine.pricing_intelligence import alerts as alerts_module
from balance_pricing_engine.pricing_intelligence.alerts import (
    TelegramAlertSink,
    WebhookAlertSink,
    dispatch_alerts_safely_detailed,
    plain_alert,
)


@pytest.mark.parametrize("destination", ["telegram", "webhook"])
@pytest.mark.parametrize("separator", ["\n", "\t", " "])
def test_default_poster_invalid_url_hides_malformed_credentials(
    destination, separator, monkeypatch, caplog, capsys
):
    prefix = f"private-{destination}-prefix"
    suffix = f"private-{destination}-suffix"
    credential = f"{prefix}{separator}{suffix}"
    expected_url = (
        f"https://api.telegram.org/bot{credential}/sendMessage"
        if destination == "telegram"
        else f"https://hooks.example.test/services/{credential}"
    )
    requested_urls = []

    def rejecting_urlopen(request, *, timeout):
        # Let the real default poster build urllib's Request. Stop at the
        # transport boundary, before any network access, like http.client's
        # invalid-path check, whose exception includes the malformed URL.
        requested_urls.append(request.full_url)
        raise InvalidURL(f"URL contains control characters: {request.full_url!r}")

    monkeypatch.setattr(alerts_module, "urlopen", rejecting_urlopen)
    sink = (
        TelegramAlertSink(credential, "private-chat")
        if destination == "telegram"
        else WebhookAlertSink(expected_url)
    )
    with caplog.at_level(logging.WARNING, logger=alerts_module.__name__):
        delivered = dispatch_alerts_safely_detailed([plain_alert("title", "body")], sink)

    assert requested_urls == [expected_url]
    assert separator in requested_urls[0]
    assert delivered == []
    output = capsys.readouterr()
    surfaces = caplog.text + repr([record.__dict__ for record in caplog.records])
    surfaces += output.out + output.err + repr(delivered)
    for private_text in (prefix, suffix, credential, expected_url, "private-chat"):
        assert private_text not in surfaces
    assert f"destination={destination}" in caplog.text
    assert "category=send_failed" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("destination", ["telegram", "webhook"])
@pytest.mark.parametrize("separator", ["", "\n", " \t "])
def test_delivery_failures_discard_exception_text(destination, separator, caplog, capsys):
    token = "123456:test-token-private"
    webhook = "https://hooks.example.test/services/private-webhook-secret"
    error_text = f"destination invalid{separator}{token}{separator}{webhook}"

    def failing_poster(url, payload):
        raise ConnectionError(error_text)

    sink = (
        TelegramAlertSink(token, "private-chat", poster=failing_poster)
        if destination == "telegram"
        else WebhookAlertSink(webhook, poster=failing_poster)
    )
    alert = plain_alert("Untrusted private-alert-title", "body")
    with caplog.at_level(logging.WARNING, logger=alerts_module.__name__):
        delivered = dispatch_alerts_safely_detailed([alert], sink)

    assert delivered == []
    output = capsys.readouterr()
    surfaces = caplog.text + repr([record.__dict__ for record in caplog.records])
    surfaces += output.out + output.err + repr(delivered)
    for private_text in (token, webhook, "private-chat", "private-alert-title", error_text):
        assert private_text not in surfaces
    assert "ConnectionError" in caplog.text
    assert f"destination={destination}" in caplog.text
    assert "category=send_failed" in caplog.text
    assert "alert=1" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_failure_never_formats_exception_or_uses_untrusted_exception_class(caplog, capsys):
    class UnprintableError(Exception):
        def __str__(self):
            raise AssertionError("Exception formatting must never run")

    UnprintableError.__name__ = "private-token\ninjected-class-name"

    def failing_poster(url, payload):
        raise UnprintableError("private-exception-message")

    sink = WebhookAlertSink("https://hooks.example.test/private", poster=failing_poster)
    assert dispatch_alerts_safely_detailed([plain_alert("title", "body")], sink) == []

    output = capsys.readouterr()
    surfaces = caplog.text + repr([record.__dict__ for record in caplog.records])
    surfaces += output.out + output.err
    assert "private-" not in surfaces
    assert "exception=Exception" in caplog.text


def test_thread_start_failure_discards_exception_text(monkeypatch, caplog, capsys):
    def raising_start(self):
        raise RuntimeError("invalid URL\nprivate-webhook-secret private-telegram-token")

    monkeypatch.setattr(alerts_module.threading.Thread, "start", raising_start)
    sink = WebhookAlertSink("https://hooks.example.test/private", poster=lambda url, body: None)

    assert dispatch_alerts_safely_detailed([plain_alert("title", "body")], sink) == []

    output = capsys.readouterr()
    surfaces = caplog.text + repr([record.__dict__ for record in caplog.records])
    surfaces += output.out + output.err
    assert "private-" not in surfaces
    assert "category=thread_start_failed" in caplog.text
    assert "exception=RuntimeError" in caplog.text


def test_deadline_logs_use_fixed_context_instead_of_alert_text(caplog):
    release = threading.Event()
    finished = threading.Event()

    def slow_poster(url, payload):
        try:
            release.wait(timeout=2)
        finally:
            finished.set()

    sink = TelegramAlertSink("private-token", "private-chat", poster=slow_poster)
    alert = plain_alert("private-title\ninjected-line", "body")
    try:
        assert dispatch_alerts_safely_detailed([alert, alert], sink, deadline_seconds=0.02) == []
    finally:
        release.set()
        assert finished.wait(timeout=1)

    surfaces = caplog.text + repr([record.__dict__ for record in caplog.records])
    assert "private-" not in surfaces
    assert "category=send_timeout" in caplog.text
    assert "category=dispatch_deadline_exceeded" in caplog.text
