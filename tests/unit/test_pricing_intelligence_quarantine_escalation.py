"""DATA-002: a pending quarantine must not stay invisible.

Distinct collection runs age an unresolved quarantine; at ``escalation_after_runs`` the run is
degraded (exit 2), health reports an escalation, and one alert is raised -- not one per retry.
"""

from __future__ import annotations

from datetime import timedelta

from quarantine_support import TD, Market, at, spike_market

from balance_pricing_engine.pricing_intelligence.config import QuarantinePolicy
from balance_pricing_engine.pricing_intelligence.health import assess_system_health
from balance_pricing_engine.pricing_intelligence.orchestration import (
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_TOTAL_FAILURE,
)
from balance_pricing_engine.pricing_intelligence.quarantine import ReviewAction

EVIDENCE = "https://rates.example.test/asb/rate-card.pdf"


def escalation_titles(market):
    return [t for t in market.sink.titles() if "QUARANTINE UNRESOLVED" in t.upper()]


def decide(market, action, *, when=at(9, 1)):
    kwargs = dict(reviewer="alice", reason="checked", reviewed_at=when)
    if action is ReviewAction.APPROVE:
        kwargs["evidence_ref"] = EVIDENCE
    return market.service.review(market.only_pending().event_id, action, **kwargs)


# ------------------------------------------------------------- visibility / age


def test_t23_pending_quarantine_is_visible_in_health_output():
    market = spike_market()
    health = assess_system_health(market.service, product_type=TD, now=at(2, 1))
    status = health.quarantine
    assert status is not None and status.pending_count == 1
    assert status.affected_banks == ("ASB",)
    assert status.escalated == ()
    assert status.oldest_age_hours is not None and 0 <= status.oldest_age_hours < 24
    asb = next(bank for bank in health.banks if bank.bank == "ASB")
    assert asb.freshness.quarantined is True


def test_t24_repeated_distinct_runs_advance_the_quarantine_age():
    market = spike_market()
    ages = [market.only_pending().runs_pending]
    for day in (3, 4, 5):
        market.run(day)
        ages.append(market.only_pending().runs_pending)
    assert ages == [1, 2, 3, 4]
    assert market.only_pending().age_hours > 3 * 24 - 1  # since the first quarantined run


def test_t25_replaying_the_same_run_id_does_not_age_the_quarantine():
    market = spike_market()  # run-2 quarantines the reading
    for _ in range(3):
        market.run(2, run_id="run-2")  # retries of the very same run
    assert market.only_pending().runs_pending == 1


def test_a_bank_that_failed_to_collect_is_not_aged():
    market = spike_market()
    market.fail("ASB")
    market.run(3)
    market.fail("ASB", False)
    assert market.only_pending().runs_pending == 1


# ------------------------------------------------------------------- exit code


def test_t26_quarantine_beyond_threshold_degrades_an_otherwise_successful_run():
    market = spike_market()  # run 2 -> 1 run pending (default threshold is 3)
    assert market.run(3).code == EXIT_OK  # 2 runs
    result = market.run(4)  # 3 runs: at the threshold
    assert result.code == EXIT_PARTIAL_FAILURE
    assert result.run_record.status == "PARTIAL_SUCCESS"
    assert result.quarantine is not None and len(result.quarantine.escalated) == 1
    assert "quarantine" in (result.run_record.error_summary or "").lower()
    assert market.run(5).code == EXIT_PARTIAL_FAILURE  # stays degraded while unresolved


def test_threshold_is_configurable():
    market = spike_market(quarantine=QuarantinePolicy(escalation_after_runs=2))
    assert market.run(3).code == EXIT_PARTIAL_FAILURE


def test_first_detection_keeps_existing_warnings_and_does_not_degrade():
    market = Market(TD)
    market.run(0)
    market.run(1)
    market.set("ASB", 4.90)
    result = market.run(2)
    assert result.code == EXIT_OK
    assert result.outcome.validation.suspicious_keys  # the established warning still fires
    assert any(change.suspicious for change in result.outcome.changes)


def test_total_failure_keeps_precedence_over_a_quarantine_escalation():
    market = spike_market()
    market.run(3)
    market.run(4)  # escalated
    for bank in ("ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"):
        market.fail(bank)
    assert market.run(5).code == EXIT_TOTAL_FAILURE


def test_a_retried_escalating_run_keeps_its_recorded_status():
    market = spike_market()
    market.run(3)
    first = market.run(4)
    retry = market.run(4, run_id="run-4")
    assert first.code == retry.code == EXIT_PARTIAL_FAILURE
    assert retry.run_record.status == "PARTIAL_SUCCESS"


# --------------------------------------------------------------------- health


def test_health_reports_the_escalation_until_resolved():
    market = spike_market()
    market.run(3)
    market.run(4)
    status = assess_system_health(market.service, product_type=TD, now=at(4, 1)).quarantine
    assert status.pending_count == 1 and len(status.escalated) == 1
    assert status.escalated[0].runs_pending == 3 and status.escalated[0].bank == "ASB"


# ---------------------------------------------------------------------- alerts


def test_t27_escalation_alerts_are_sent_once_not_on_every_run_or_retry():
    market = spike_market()
    market.run(3)
    assert escalation_titles(market) == []
    market.run(4)  # threshold reached
    assert len(escalation_titles(market)) == 1
    market.run(4, run_id="run-4")  # retry
    market.run(5)
    market.run(6)
    assert len(escalation_titles(market)) == 1
    body = next(a for a in market.sink.alerts if "UNRESOLVED" in a.title.upper()).body
    assert "ASB" in body and "TERM DEPOSIT" in body.upper()
    assert "token" not in body.lower() and "secret" not in body.lower()


def test_a_failed_escalation_delivery_is_retried_next_run():
    market = spike_market()
    market.run(3)
    original_send = market.sink.send
    market.sink.send = lambda alert: (_ for _ in ()).throw(RuntimeError("sink down"))
    market.run(4)
    market.sink.send = original_send
    market.run(5)
    assert len(escalation_titles(market)) == 1


def test_dry_run_neither_alerts_nor_records_delivery():
    market = spike_market()
    market.run(3)
    from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle

    run_operational_cycle(
        market.service, product_type=TD, captured_at_utc=at(4), run_id="dry", dry_run=True
    )
    assert escalation_titles(market) == []
    market.run(4)
    assert len(escalation_titles(market)) == 1


# ------------------------------------------------------------------ resolution


def test_t28_approval_clears_the_active_escalation_and_keeps_history():
    market = spike_market()
    market.run(3)
    market.run(4)
    decide(market, ReviewAction.APPROVE, when=at(4, 2))
    result = market.run(5)
    assert result.code == EXIT_OK
    status = assess_system_health(market.service, product_type=TD, now=at(5, 1)).quarantine
    assert status.pending_count == 0 and status.escalated == ()
    assert len(market.ledger_rows()) == 1  # history preserved
    asb = next(
        b
        for b in assess_system_health(market.service, product_type=TD, now=at(5, 1)).banks
        if b.bank == "ASB"
    )
    assert asb.freshness.quarantined is False


def test_t28_rejection_also_clears_it_and_stops_reporting_it_unresolved():
    market = spike_market()
    market.run(3)
    market.run(4)
    decide(market, ReviewAction.REJECT, when=at(4, 2))
    for day in (5, 6):
        assert market.run(day).code == EXIT_OK  # the page still says 4.90; nothing to review
    health = assess_system_health(market.service, product_type=TD, now=at(6, 1))
    assert health.quarantine.pending_count == 0
    asb = next(b for b in health.banks if b.bank == "ASB")
    assert asb.freshness.quarantined is False


def test_resolution_sends_one_recovery_notice():
    market = spike_market()
    market.run(3)
    market.run(4)
    decide(market, ReviewAction.REJECT, when=at(4, 2))
    market.run(5)
    market.run(6)
    resolved = [t for t in market.sink.titles() if "QUARANTINE RESOLVED" in t.upper()]
    assert len(resolved) == 1


def test_oldest_pending_age_tracks_the_oldest_case():
    market = Market(TD, terms=(12, 24))
    market.run(0)
    market.run(1)
    market.set("ASB", 4.90, term=12)
    market.run(2)
    market.set("ASB", 4.95, term=24)
    market.run(3)
    status = assess_system_health(market.service, product_type=TD, now=at(3, 1)).quarantine
    assert status.pending_count == 2
    assert status.oldest_age_hours >= timedelta(days=1, hours=1).total_seconds() / 3600 - 0.01
