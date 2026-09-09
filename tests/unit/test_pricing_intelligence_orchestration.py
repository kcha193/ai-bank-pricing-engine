"""run_operational_cycle(): run classification, health escalation, dedup,
RunRecord persistence, and dry-run semantics -- driven entirely by fixtures
and injected sinks, no live bank sites or real Telegram/webhook calls."""

from datetime import datetime, timedelta, timezone
import logging

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
    EXIT_OK,
    HEALTH_ALERT,
    HEALTH_NONE,
    HEALTH_RECOVERY,
    HEALTH_WARNING,
    ScenarioResult,
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


def test_a_change_persisted_but_never_alerted_is_replayed_on_a_later_run(
    tmp_path, fixture_fetcher
):
    """If the process exits after service.collect() commits a change but
    before run_operational_cycle reaches alert dispatch (a crash mid-run,
    simulated here by calling service.collect() directly instead of going
    through run_operational_cycle), the change must not be lost forever --
    the next successful cycle must still deliver it, even though its own
    fresh collect() sees no new change (the crashed run's collect() already
    persisted the new rate as the baseline)."""
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

        # Simulate the crash: collect() alone persists the ANZ change, but
        # nothing ever reaches alert dispatch (that only happens inside
        # run_operational_cycle, which is never called here).
        service._fetcher = fixture_fetcher(mutate=mutate)
        crashed_outcome = service.collect(captured_at_utc=DAY_TWO)
        assert any(c.is_rate_move for c in crashed_outcome.changes)
        assert sink.alerts == []

        # The next scheduled run, one day later, sees no *new* change --
        # DAY_TWO's mutated rate is already the stored baseline -- but must
        # still deliver the change orphaned by the simulated crash.
        result = run_operational_cycle(
            service,
            captured_at_utc=DAY_TWO + timedelta(days=1),
            alert_sinks={"test": sink},
        )

    assert len(result.outcome.changes) == 0, "no fresh change this run -- the replay is the point"
    assert len(sink.alerts) == 1
    assert "ANZ" in sink.alerts[0].title or "ANZ" in sink.alerts[0].body


def test_a_suspicious_change_persisted_but_never_alerted_is_replayed_too(
    tmp_path, fixture_fetcher
):
    """The undelivered-change replay must not silently drop a suspicious
    move: the live path already delivers one (with its explicit "FLAGGED
    SUSPICIOUS -- verify against the source page" warning, see
    alerts.format_alert), so a suspicious change orphaned by a crash before
    dispatch must still be replayed the same way, not lost -- that would
    hide exactly the alert a parser fault most needs a human to see."""
    config = _config(tmp_path)
    sink = CollectingAlertSink()

    def mutate_to_suspicious(bank, html):
        if bank == "ANZ":
            # A move far larger than suspicious_change_bps (75 bps default).
            return html.replace("<td>1 year</td><td>4.79%", "<td>1 year</td><td>8.49%")
        return html

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)

        # Simulate the crash: collect() alone persists the suspicious ANZ
        # change, but nothing ever reaches alert dispatch.
        service._fetcher = fixture_fetcher(mutate=mutate_to_suspicious)
        crashed_outcome = service.collect(captured_at_utc=DAY_TWO)
        assert any(c.suspicious for c in crashed_outcome.changes)
        assert sink.alerts == []

        result = run_operational_cycle(
            service,
            captured_at_utc=DAY_TWO + timedelta(days=1),
            alert_sinks={"test": sink},
        )

    assert len(result.outcome.changes) == 0
    assert len(sink.alerts) == 1
    assert "FLAGGED SUSPICIOUS" in sink.alerts[0].body


def test_dispatch_rendered_alerts_only_records_confirmed_sends(tmp_path, make_record):
    """A send that fails must not be recorded as notified -- otherwise a
    retried run's was_notified() dedup check would permanently suppress a
    change that was never actually delivered (see
    dispatch_alerts_safely_detailed and its use in _dispatch_rendered_alerts)."""
    from balance_pricing_engine.pricing_intelligence.changes import detect_changes
    from balance_pricing_engine.pricing_intelligence.orchestration import (
        _dispatch_rendered_alerts,
    )

    change_ok = detect_changes(
        [make_record(bank="ANZ", rate_pct=4.99)],
        [make_record(bank="ANZ", rate_pct=4.79, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )[0]
    change_fails = detect_changes(
        [make_record(bank="BNZ", rate_pct=4.95)],
        [make_record(bank="BNZ", rate_pct=4.85, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )[0]
    from balance_pricing_engine.pricing_intelligence.alerts import format_alerts

    alerts = format_alerts([change_ok, change_fails])
    ok_bank = next(a for a in alerts if a.change.bank == "ANZ")
    failing_bank = next(a for a in alerts if a.change.bank == "BNZ")

    class PartiallyFailingSink:
        def send(self, alert) -> None:
            if alert.change is not None and alert.change.bank == "BNZ":
                raise RuntimeError("simulated webhook outage")

    store = ObservationStore(str(tmp_path / "obs.db"))
    try:
        total_sent = _dispatch_rendered_alerts(store, alerts, {"test": PartiallyFailingSink()})

        assert total_sent == 1, "only the confirmed send counts toward the total"
        assert store.was_notified(ok_bank.change, "test") is True
        assert store.was_notified(failing_bank.change, "test") is False, (
            "a failed send must remain eligible for retry, not be marked as delivered"
        )
    finally:
        store.close()


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


def test_health_warning_still_logs_with_no_alert_sinks_configured(tmp_path, fixture_fetcher):
    """warning_after_failures escalation is a structured log, not a delivery
    -- it must fire even with no webhook/Telegram configured (the documented
    default), not only when run_operational_cycle happens to have a sink.

    A directly-attached handler is used instead of caplog: configure_json_logging()
    (called by an earlier test's workflow.main() in the same process) sets
    propagate = False on the balance_pricing_engine logger hierarchy, which
    stops records from reaching caplog's root-based capture -- same rationale
    as tests/unit/test_scheduled_collection_workflow.py's own tests of this."""
    from balance_pricing_engine.pricing_intelligence import orchestration as orchestration_module

    config = _config(
        tmp_path, health=HealthThresholds(warning_after_failures=2, alert_after_failures=3)
    )

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    orchestration_module.logger.addHandler(handler)
    try:
        for day in range(2):
            fetcher = fixture_fetcher(failing_banks=["Westpac"])
            with PricingIntelligenceService(
                config=config, store=ObservationStore(config.database_path), fetcher=fetcher
            ) as service:
                run_operational_cycle(
                    service, captured_at_utc=DAY_ONE + timedelta(days=day), alert_sinks={}
                )
    finally:
        orchestration_module.logger.removeHandler(handler)

    assert any(
        "collector health" in record.getMessage() and "Westpac" in record.getMessage()
        for record in records
    ), "the warning-level escalation must log regardless of configured sinks"


def test_health_alerts_for_several_banks_are_batched_into_one_dispatch_call(tmp_path, monkeypatch):
    """Several banks escalating in the same cycle must be dispatched in one
    dispatch_alerts_safely call per sink, not one call per bank -- a
    per-bank call gives each bank its own fresh dispatch deadline, so N
    banks escalating against one hung sink could otherwise serialise into
    N * deadline seconds of blocking while the run's single-instance lock
    is still held."""
    from balance_pricing_engine.pricing_intelligence import orchestration as orchestration_module
    from balance_pricing_engine.pricing_intelligence.orchestration import _dispatch_health_alerts

    calls = []

    def fake_dispatch(alerts, sink, **kwargs):
        calls.append(list(alerts))
        return len(alerts)

    monkeypatch.setattr(orchestration_module, "dispatch_alerts_safely", fake_dispatch)

    store = ObservationStore(str(tmp_path / "obs.db"))
    thresholds = HealthThresholds(warning_after_failures=1, alert_after_failures=1)
    try:
        _dispatch_health_alerts(
            store,
            ProductType.HOME_LOAN,
            [("ANZ", False, "boom"), ("BNZ", False, "boom")],
            thresholds,
            {"test": CollectingAlertSink()},
            DAY_ONE,
        )
    finally:
        store.close()

    assert len(calls) == 1, "both banks' escalation alerts must share a single dispatch call"
    assert len(calls[0]) == 2
    assert {alert.body.split("\n")[0] for alert in calls[0]} == {
        "ANZ collector failed 1 consecutive runs.",
        "BNZ collector failed 1 consecutive runs.",
    }


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


# --------------------------------------------------- scenario-trigger wiring


def _fake_scenario_result(term_months) -> ScenarioResult:
    return ScenarioResult(
        data=[{"label": "No change", "our_rate": 0.0549}],
        alert_block="PRICING ENGINE\n\nNo-change optimum:5.49%",
    )


def _mutate_anz_one_year(bank, html):
    if bank == "ANZ":
        return html.replace("<td>1 year</td><td>4.79%", "<td>1 year</td><td>4.59%")
    return html


def test_scenario_trigger_fn_is_called_for_a_genuine_rate_move(tmp_path, fixture_fetcher):
    config = _config(tmp_path)
    calls = []

    def trigger_fn(terms, snapshot):
        calls.append((tuple(terms), snapshot))
        return {term: _fake_scenario_result(term) for term in terms}

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=_mutate_anz_one_year)
        run_operational_cycle(
            service, captured_at_utc=DAY_TWO, alert_sinks={}, scenario_trigger_fn=trigger_fn
        )

    assert len(calls) == 1
    assert calls[0][0] == (12,)


def test_a_failing_scenario_trigger_fn_does_not_abort_the_cycle(tmp_path, fixture_fetcher):
    """A scenario_trigger_fn failure -- e.g. no external competitor peer
    remains once our_bank is excluded from the only bank that moved this run
    -- is a reachable, documented case, not a bug in the callback. It must
    not cost the run its alerts, collector-health update, run record, or
    summary, all of which are otherwise valid off a successful collect()."""
    config = _config(tmp_path)
    sink = CollectingAlertSink()

    def failing_trigger_fn(terms, snapshot):
        raise RuntimeError("simulated: no external peer remains for this term")

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=_mutate_anz_one_year)
        result = run_operational_cycle(
            service,
            captured_at_utc=DAY_TWO,
            alert_sinks={"test": sink},
            scenario_trigger_fn=failing_trigger_fn,
        )

    assert result.code == EXIT_OK
    assert result.run_record is not None
    assert len(sink.alerts) == 1, "the rate-move alert must still be delivered"
    assert result.outcome.report.any_success


def test_scenario_trigger_fn_is_not_called_with_no_changes(tmp_path, fixture_fetcher):
    config = _config(tmp_path)
    calls = []

    def trigger_fn(terms, snapshot):
        calls.append(terms)
        return {}

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        run_operational_cycle(
            service, captured_at_utc=DAY_ONE, alert_sinks={}, scenario_trigger_fn=trigger_fn
        )

    assert calls == []


def test_scenario_trigger_fn_is_not_called_for_a_suspicious_change(tmp_path, fixture_fetcher):
    """A suspicious movement must not silently trigger a trusted-looking scenario run."""
    config = _config(tmp_path)
    calls = []

    def trigger_fn(terms, snapshot):
        calls.append(terms)
        return {}

    def mutate_to_suspicious(bank, html):
        if bank == "ANZ":
            # A move far larger than suspicious_change_bps (75 bps default).
            return html.replace("<td>1 year</td><td>4.79%", "<td>1 year</td><td>8.49%")
        return html

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=mutate_to_suspicious)
        result = run_operational_cycle(
            service, captured_at_utc=DAY_TWO, alert_sinks={}, scenario_trigger_fn=trigger_fn
        )

    assert any(change.suspicious for change in result.outcome.changes)
    assert calls == []


def test_no_scenario_trigger_fn_means_no_scenario_runs_persisted(tmp_path, fixture_fetcher):
    config = _config(tmp_path)
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=_mutate_anz_one_year)
        result = run_operational_cycle(service, captured_at_utc=DAY_TWO, alert_sinks={})

    with ObservationStore(config.database_path) as store:
        assert store.get_scenario_runs(result.run_record.run_id) == []


def test_triggered_scenario_is_persisted_and_linked_to_the_run(tmp_path, fixture_fetcher):
    config = _config(tmp_path)

    def trigger_fn(terms, snapshot):
        return {term: _fake_scenario_result(term) for term in terms}

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=_mutate_anz_one_year)
        result = run_operational_cycle(
            service, captured_at_utc=DAY_TWO, alert_sinks={}, scenario_trigger_fn=trigger_fn
        )

    with ObservationStore(config.database_path) as store:
        runs = store.get_scenario_runs(result.run_record.run_id)
    assert len(runs) == 1
    assert runs[0]["term_months"] == 12
    assert runs[0]["scenarios"] == [{"label": "No change", "our_rate": 0.0549}]


def test_triggered_scenario_enriches_the_rate_move_alert(tmp_path, fixture_fetcher):
    config = _config(tmp_path)
    sink = CollectingAlertSink()

    def trigger_fn(terms, snapshot):
        return {term: _fake_scenario_result(term) for term in terms}

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=_mutate_anz_one_year)
        run_operational_cycle(
            service,
            captured_at_utc=DAY_TWO,
            alert_sinks={"telegram": sink},
            scenario_trigger_fn=trigger_fn,
        )

    assert len(sink.alerts) == 1
    assert "PRICING ENGINE" not in sink.alerts[0].title  # still the rate-change title
    assert "NZ MORTGAGE RATE CHANGE" in sink.alerts[0].title
    assert "PRICING ENGINE" in sink.alerts[0].body
    assert "No-change optimum:5.49%" in sink.alerts[0].body


def test_dry_run_still_previews_scenarios_but_persists_nothing(tmp_path, fixture_fetcher):
    config = _config(tmp_path)
    calls = []

    def trigger_fn(terms, snapshot):
        calls.append(tuple(terms))
        return {term: _fake_scenario_result(term) for term in terms}

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(mutate=_mutate_anz_one_year)
        run_operational_cycle(
            service,
            captured_at_utc=DAY_TWO,
            dry_run=True,
            scenario_trigger_fn=trigger_fn,
        )

    assert calls == [(12,)]
    with ObservationStore(config.database_path) as store:
        assert store.get_recent_scenario_runs() == []


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
