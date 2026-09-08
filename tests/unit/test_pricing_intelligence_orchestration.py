"""run_operational_cycle(): run classification, health escalation, dedup,
RunRecord persistence, and dry-run semantics -- driven entirely by fixtures
and injected sinks, no live bank sites or real Telegram/webhook calls."""

from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.competitor_rates.collectors import COLLECTOR_CLASSES
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.alerts import CollectingAlertSink
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    HealthThresholds,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.orchestration import (
    HEALTH_ALERT,
    HEALTH_NONE,
    HEALTH_RECOVERY,
    HEALTH_WARNING,
    classify_health_escalation,
    run_operational_cycle,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import CollectorHealth, ObservationStore

DAY_ONE = datetime(2026, 8, 20, tzinfo=timezone.utc)
DAY_TWO = DAY_ONE + timedelta(days=1)


def _config(tmp_path, **overrides):
    return PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
        **overrides,
    )


def test_a_clean_run_is_success(tmp_path, fixture_fetcher):
    config = _config(tmp_path)
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        result = run_operational_cycle(service, captured_at_utc=DAY_ONE, alert_sinks={})

    assert result.status == "SUCCESS"
    assert result.code == 0
    assert result.run_record is not None
    assert result.run_record.status == "SUCCESS"
    assert set(result.run_record.banks_successful) == {
        c.bank for c in COLLECTOR_CLASSES
    }
    assert result.run_record.banks_failed == ()


def test_every_bank_failing_is_a_failed_run_record(tmp_path, fixture_fetcher):
    config = _config(tmp_path)
    fetcher = fixture_fetcher(failing_banks=[c.bank for c in COLLECTOR_CLASSES])
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fetcher
    ) as service:
        result = run_operational_cycle(service, captured_at_utc=DAY_ONE, alert_sinks={})

    assert result.status == "FAILED"
    assert result.code == 1
    assert result.run_record.status == "FAILED"
    assert result.run_record.banks_successful == ()
    assert result.run_record.error_summary is not None


def test_one_bank_failing_is_a_partial_success(tmp_path, fixture_fetcher):
    config = _config(tmp_path)
    fetcher = fixture_fetcher(failing_banks=["Westpac"])
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fetcher
    ) as service:
        result = run_operational_cycle(service, captured_at_utc=DAY_ONE, alert_sinks={})

    assert result.status == "PARTIAL_SUCCESS"
    assert result.code == 2
    assert "Westpac" in result.run_record.banks_failed
    assert "Westpac" not in result.run_record.banks_successful


def test_dry_run_persists_no_run_record_and_no_observations(tmp_path, fixture_fetcher):
    config = _config(tmp_path)
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        result = run_operational_cycle(service, captured_at_utc=DAY_ONE, dry_run=True)

        assert result.run_record is None
        assert service.store.get_latest_run_record() is None
        assert service.store.get_current_observations() == []


def test_dry_run_routes_alerts_to_the_console_not_real_sinks(tmp_path, fixture_fetcher, monkeypatch):
    monkeypatch.delenv("PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", raising=False)

    def urlopen_must_not_be_called(*args, **kwargs):
        raise AssertionError("a dry run must never reach a real alert channel")

    from balance_pricing_engine.pricing_intelligence import alerts as alerts_module

    monkeypatch.setattr(alerts_module, "urlopen", urlopen_must_not_be_called)

    config = _config(tmp_path)
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(
            mutate=lambda bank, html: html.replace(
                "<td>1 year</td><td>4.79%", "<td>1 year</td><td>4.59%"
            )
            if bank == "ANZ"
            else html
        )
        # Even with a real webhook URL configured, dry_run must not call it --
        # verified by urlopen raising above if it were ever reached.
        monkeypatch.setenv("PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", "https://hooks.example/x")
        result = run_operational_cycle(service, captured_at_utc=DAY_TWO, dry_run=True)

    assert len(result.outcome.changes) == 1
    assert result.alerts_sent == 1  # delivered to the console sink instead


def test_no_change_run_sends_no_alert(tmp_path, fixture_fetcher):
    config = _config(tmp_path)
    sink = CollectingAlertSink()
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        run_operational_cycle(
            service, captured_at_utc=DAY_ONE, alert_sinks={"test": sink}
        )
        result = run_operational_cycle(
            service, captured_at_utc=DAY_TWO, alert_sinks={"test": sink}
        )

    assert len(result.outcome.changes) == 0
    assert result.alerts_sent == 0
    assert sink.alerts == []


def test_a_reprice_is_delivered_exactly_once_across_retries(tmp_path, fixture_fetcher):
    """The same already-detected change must not be re-sent if the same
    run_id's cycle is re-invoked (e.g. a crashed-then-retried run)."""
    config = _config(tmp_path)
    sink = CollectingAlertSink()

    def mutate(bank, html):
        if bank == "ANZ":
            return html.replace("<td>1 year</td><td>4.79%", "<td>1 year</td><td>4.59%")
        return html

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=mutate)
        first = run_operational_cycle(
            service, captured_at_utc=DAY_TWO, alert_sinks={"test": sink}
        )
        assert len(sink.alerts) == 1

        second = run_operational_cycle(
            service,
            captured_at_utc=DAY_TWO,
            run_id=first.run_record.run_id,
            alert_sinks={"test": sink},
        )

    assert first.run_record.run_id == second.run_record.run_id
    assert len(sink.alerts) == 1, "the retried run must not re-deliver the same change"


def test_collector_health_tracks_per_bank_streaks(tmp_path, fixture_fetcher):
    config = _config(tmp_path)
    fetcher = fixture_fetcher(failing_banks=["Westpac"])
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fetcher
    ) as service:
        run_operational_cycle(service, captured_at_utc=DAY_ONE, alert_sinks={})
        health = {h.bank: h for h in service.store.get_collector_health()}

    assert health["Westpac"].consecutive_failures == 1
    assert health["ANZ"].consecutive_failures == 0


@pytest.mark.parametrize(
    "previous_streak, current_streak, expected",
    [
        (0, 0, HEALTH_NONE),
        (0, 1, HEALTH_NONE),
        (1, 2, HEALTH_WARNING),
        (2, 3, HEALTH_ALERT),
        (3, 4, HEALTH_ALERT),
        (3, 0, HEALTH_RECOVERY),
        (1, 0, HEALTH_NONE),  # recovered from a streak that never escalated
    ],
)
def test_classify_health_escalation_policy(previous_streak, current_streak, expected):
    thresholds = HealthThresholds(warning_after_failures=2, alert_after_failures=3)
    previous = CollectorHealth(
        bank="ANZ",
        product_type=ProductType.HOME_LOAN,
        consecutive_failures=previous_streak,
        last_success_at=None,
        last_failure_at=None,
        last_error=None,
        last_alert_sent_at=None,
    )
    current = CollectorHealth(
        bank="ANZ",
        product_type=ProductType.HOME_LOAN,
        consecutive_failures=current_streak,
        last_success_at=None,
        last_failure_at=None,
        last_error=None,
        last_alert_sent_at=None,
    )

    assert classify_health_escalation(thresholds, previous, current) == expected


def test_three_consecutive_failures_sends_a_telegram_alert(tmp_path, fixture_fetcher):
    config = _config(tmp_path, health=HealthThresholds(warning_after_failures=2, alert_after_failures=3))
    sink = CollectingAlertSink()

    for day in range(3):
        fetcher = fixture_fetcher(failing_banks=["Westpac"])
        with PricingIntelligenceService(
            config=config, store=ObservationStore(config.database_path), fetcher=fetcher
        ) as service:
            run_operational_cycle(
                service,
                captured_at_utc=DAY_ONE + timedelta(days=day),
                alert_sinks={"telegram": sink},
            )

    health_alerts = [a for a in sink.alerts if "COLLECTOR ISSUE" in a.title]
    assert len(health_alerts) == 1
    assert "Westpac" in health_alerts[0].body
    assert "3 consecutive" in health_alerts[0].body


def test_two_consecutive_failures_does_not_alert_only_warns(tmp_path, fixture_fetcher, caplog):
    config = _config(tmp_path, health=HealthThresholds(warning_after_failures=2, alert_after_failures=3))
    sink = CollectingAlertSink()

    for day in range(2):
        fetcher = fixture_fetcher(failing_banks=["Westpac"])
        with PricingIntelligenceService(
            config=config, store=ObservationStore(config.database_path), fetcher=fetcher
        ) as service:
            run_operational_cycle(
                service,
                captured_at_utc=DAY_ONE + timedelta(days=day),
                alert_sinks={"telegram": sink},
            )

    assert sink.alerts == []


def test_recovery_alert_fires_after_an_escalated_streak(tmp_path, fixture_fetcher):
    config = _config(tmp_path, health=HealthThresholds(warning_after_failures=2, alert_after_failures=3))
    sink = CollectingAlertSink()

    for day in range(3):
        fetcher = fixture_fetcher(failing_banks=["Westpac"])
        with PricingIntelligenceService(
            config=config, store=ObservationStore(config.database_path), fetcher=fetcher
        ) as service:
            run_operational_cycle(
                service,
                captured_at_utc=DAY_ONE + timedelta(days=day),
                alert_sinks={"telegram": sink},
            )

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        run_operational_cycle(
            service, captured_at_utc=DAY_ONE + timedelta(days=3), alert_sinks={"telegram": sink}
        )

    recovery_alerts = [a for a in sink.alerts if "recovered" in a.title]
    assert len(recovery_alerts) == 1
    assert "Westpac" in recovery_alerts[0].title


def test_a_broken_telegram_sink_does_not_fail_the_run(tmp_path, fixture_fetcher):
    class BrokenSink:
        def send(self, alert):
            raise ConnectionError("telegram unreachable")

    config = _config(tmp_path)

    def mutate(bank, html):
        if bank == "ANZ":
            return html.replace("<td>1 year</td><td>4.79%", "<td>1 year</td><td>4.59%")
        return html

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=mutate)
        result = run_operational_cycle(
            service, captured_at_utc=DAY_TWO, alert_sinks={"telegram": BrokenSink()}
        )

    assert result.status == "SUCCESS"
    assert result.alerts_sent == 0
