"""Webhook alert delivery, driven entirely by injected posters -- no network."""

from datetime import datetime, timezone
import threading
import time as time_module

import pytest

from balance_pricing_engine.pricing_intelligence.alerts import (
    ALERT_WEBHOOK_URL_SECRET_NAME,
    WebhookAlertSink,
    build_webhook_sink_from_env,
    dispatch_alerts_safely,
    format_alerts,
)
from balance_pricing_engine.pricing_intelligence.changes import detect_changes
from balance_pricing_engine.secrets.provider import StaticSecretsProvider

DAY_TWO = datetime(2026, 9, 3, tzinfo=timezone.utc)


def build_change(make_record, previous=4.99, current=4.79):
    return detect_changes(
        [make_record(rate_pct=previous)],
        [make_record(rate_pct=current, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )[0]


def test_webhook_sink_posts_a_json_payload_with_the_rendered_alert(make_record):
    change = build_change(make_record)
    alert = format_alerts([change])[0]
    captured = []

    def fake_poster(url, payload_bytes):
        import json

        captured.append((url, json.loads(payload_bytes)))

    sink = WebhookAlertSink("https://hooks.example/x", poster=fake_poster)
    sink.send(alert)

    assert len(captured) == 1
    url, payload = captured[0]
    assert url == "https://hooks.example/x"
    assert payload["text"] == alert.render()
    assert payload["bank"] == "ANZ"
    assert payload["change_type"] == "RATE_DECREASE"
    assert payload["change_bps"] == -20.0
    assert payload["suspicious"] is False


def test_text_field_is_configurable_for_discord(make_record):
    change = build_change(make_record)
    alert = format_alerts([change])[0]
    captured = []

    def fake_poster(url, payload_bytes):
        import json

        captured.append(json.loads(payload_bytes))

    sink = WebhookAlertSink("https://discord.example/x", text_field="content", poster=fake_poster)
    sink.send(alert)

    assert "content" in captured[0]
    assert "text" not in captured[0]


def test_send_raises_when_the_poster_fails(make_record):
    change = build_change(make_record)
    alert = format_alerts([change])[0]

    def failing_poster(url, payload_bytes):
        raise ConnectionError("DNS resolution failed")

    sink = WebhookAlertSink("https://dead.example/x", poster=failing_poster)

    with pytest.raises(ConnectionError):
        sink.send(alert)


def test_dispatch_safely_never_raises_when_the_webhook_is_broken(make_record):
    """Mirrors events.publisher.publish_safely(): delivery must never fail the run."""
    change = build_change(make_record)
    alerts = format_alerts([change])

    def failing_poster(url, payload_bytes):
        raise ConnectionError("boom")

    sink = WebhookAlertSink("https://dead.example/x", poster=failing_poster)

    sent = dispatch_alerts_safely(alerts, sink)

    assert sent == 0


def test_dispatch_safely_isolates_one_bad_alert_from_the_rest(make_record):
    changes = [
        build_change(make_record, previous=4.99, current=4.79),
        detect_changes(
            [make_record(bank="BNZ", rate_pct=4.95)],
            [make_record(bank="BNZ", rate_pct=4.85, observed_at=DAY_TWO)],
            detected_at=DAY_TWO,
        )[0],
    ]
    alerts = format_alerts(changes)

    calls = []

    def flaky_poster(url, payload_bytes):
        import json

        payload = json.loads(payload_bytes)
        calls.append(payload["bank"])
        if payload["bank"] == "ANZ":
            raise TimeoutError("slow webhook")

    sink = WebhookAlertSink("https://hooks.example/x", poster=flaky_poster)

    sent = dispatch_alerts_safely(alerts, sink)

    assert sent == 1, "the BNZ alert must still be delivered"
    assert set(calls) == {"ANZ", "BNZ"}


def test_dispatch_safely_stops_once_the_deadline_is_exceeded(make_record, monkeypatch):
    """A hung or rate-limited webhook must not be able to block the collection
    run indefinitely -- remaining alerts are skipped once the total time
    budget for this call is used up."""
    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    changes = [
        build_change(make_record, previous=4.99, current=4.79),
        detect_changes(
            [make_record(bank="BNZ", rate_pct=4.95)],
            [make_record(bank="BNZ", rate_pct=4.85, observed_at=DAY_TWO)],
            detected_at=DAY_TWO,
        )[0],
    ]
    alerts = format_alerts(changes)

    clock = {"now": 0.0}
    monkeypatch.setattr(alerts_module.time, "monotonic", lambda: clock["now"])

    calls = []

    def slow_poster(url, payload_bytes):
        import json

        calls.append(json.loads(payload_bytes)["bank"])
        clock["now"] += 100.0  # exceeds the deadline after the first alert

    sink = WebhookAlertSink("https://hooks.example/x", poster=slow_poster)

    sent = dispatch_alerts_safely(alerts, sink, deadline_seconds=30.0)

    assert sent == 1, "only the alert attempted before the deadline was sent"
    assert len(calls) == 1, "the second alert must not be attempted past the deadline"


def test_dispatch_safely_bounds_a_single_slow_send_by_the_deadline(make_record):
    """A per-alert check between sends is not enough: one webhook send whose
    own 429 retries take a long time must not be allowed to block dispatch
    past the deadline all on its own."""
    alerts = format_alerts([build_change(make_record)])

    release = threading.Event()

    def slow_poster(url, payload_bytes):
        release.wait(timeout=2.0)  # far longer than the deadline below

    sink = WebhookAlertSink("https://hooks.example/x", poster=slow_poster)

    start = time_module.perf_counter()
    sent = dispatch_alerts_safely(alerts, sink, deadline_seconds=0.05)
    elapsed = time_module.perf_counter() - start

    release.set()  # let the background thread finish so it doesn't outlive the test

    assert sent == 0, "the send had not completed before the deadline"
    assert elapsed < 1.0, "dispatch must not wait out the full slow send"


def test_dispatch_safely_still_reports_full_success(make_record):
    alerts = format_alerts([build_change(make_record)])
    sink = WebhookAlertSink("https://hooks.example/x", poster=lambda url, payload: None)

    assert dispatch_alerts_safely(alerts, sink) == 1


def test_no_webhook_configured_returns_none():
    assert build_webhook_sink_from_env(StaticSecretsProvider({})) is None


def test_a_configured_webhook_url_builds_a_sink():
    secrets = StaticSecretsProvider({ALERT_WEBHOOK_URL_SECRET_NAME: "https://hooks.example/x"})

    sink = build_webhook_sink_from_env(secrets)

    assert isinstance(sink, WebhookAlertSink)
    assert sink.webhook_url == "https://hooks.example/x"
    assert sink.text_field == "text"


def test_the_secret_name_matches_the_documented_env_var():
    """Locks the env var name as an interface, not an implementation detail."""
    assert ALERT_WEBHOOK_URL_SECRET_NAME == "PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL"


def test_a_custom_secret_provider_can_use_a_different_env_var_name(monkeypatch):
    secrets = StaticSecretsProvider({"CUSTOM_WEBHOOK": "https://hooks.example/custom"})

    sink = build_webhook_sink_from_env(secrets, secret_name="CUSTOM_WEBHOOK")

    assert sink.webhook_url == "https://hooks.example/custom"


def test_default_poster_retries_a_429_after_the_retry_after_delay(monkeypatch):
    """A rate-limited Slack webhook must be retried, not dropped -- otherwise a
    multi-term repricing burst loses every alert after the first 429."""
    from urllib.error import HTTPError

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    calls = []
    slept = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        if len(calls) == 1:
            raise HTTPError(
                request.full_url, 429, "Too Many Requests", {"Retry-After": "2"}, None
            )
        return FakeResponse()

    monkeypatch.setattr(alerts_module, "urlopen", fake_urlopen)

    poster = alerts_module.build_default_poster(sleep=slept.append)
    poster("https://hooks.example/x", b'{"text": "hi"}')

    assert len(calls) == 2, "the request must be retried after the 429"
    assert slept == [2.0]


def test_default_poster_gives_up_after_max_retries(monkeypatch):
    from urllib.error import HTTPError

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        raise HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(alerts_module, "urlopen", fake_urlopen)

    poster = alerts_module.build_default_poster(max_rate_limit_retries=2, sleep=lambda s: None)

    with pytest.raises(HTTPError):
        poster("https://hooks.example/x", b'{"text": "hi"}')

    assert len(calls) == 3, "the initial attempt plus 2 retries"


def test_default_poster_does_not_retry_a_non_429_error(monkeypatch):
    from urllib.error import HTTPError

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        raise HTTPError(request.full_url, 500, "Server Error", {}, None)

    monkeypatch.setattr(alerts_module, "urlopen", fake_urlopen)

    poster = alerts_module.build_default_poster(sleep=lambda s: None)

    with pytest.raises(HTTPError):
        poster("https://hooks.example/x", b'{"text": "hi"}')

    assert len(calls) == 1, "a non-429 failure must not be retried"


def test_default_poster_falls_back_to_the_default_delay_when_retry_after_is_missing(
    monkeypatch,
):
    from urllib.error import HTTPError

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    calls = []
    slept = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 429, "Too Many Requests", {}, None)
        return FakeResponse()

    monkeypatch.setattr(alerts_module, "urlopen", fake_urlopen)

    poster = alerts_module.build_default_poster(sleep=slept.append)
    poster("https://hooks.example/x", b'{"text": "hi"}')

    assert slept == [alerts_module.DEFAULT_RETRY_AFTER_SECONDS]


def test_default_poster_caps_an_excessive_retry_after(monkeypatch):
    """A malicious or misconfigured endpoint must not be able to block a run
    indefinitely by returning an enormous Retry-After value."""
    from urllib.error import HTTPError

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    calls = []
    slept = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        if len(calls) == 1:
            raise HTTPError(
                request.full_url,
                429,
                "Too Many Requests",
                {"Retry-After": "99999999"},
                None,
            )
        return FakeResponse()

    monkeypatch.setattr(alerts_module, "urlopen", fake_urlopen)

    poster = alerts_module.build_default_poster(sleep=slept.append)
    poster("https://hooks.example/x", b'{"text": "hi"}')

    assert slept == [alerts_module.MAX_RETRY_AFTER_SECONDS]


def test_default_poster_sends_a_real_http_post(monkeypatch):
    """The real (non-test-double) poster path, with urlopen substituted."""
    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["content_type"] = request.get_header("Content-type")
        captured["body"] = request.data
        return FakeResponse()

    monkeypatch.setattr(alerts_module, "urlopen", fake_urlopen)

    poster = alerts_module.build_default_poster()
    poster("https://hooks.example/x", b'{"text": "hi"}')

    assert captured["url"] == "https://hooks.example/x"
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/json"
    assert captured["body"] == b'{"text": "hi"}'
