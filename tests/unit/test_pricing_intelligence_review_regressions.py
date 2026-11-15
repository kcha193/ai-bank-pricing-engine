"""Offline regressions for PR #72's pending trust and historical activity findings."""

from datetime import timedelta
import sqlite3

import pytest
from quarantine_support import HL, TD, Market, at, make_rate

from balance_pricing_engine.pricing_intelligence.quarantine import ReviewAction
from balance_pricing_engine.pricing_intelligence.config import (
    PeerPlausibilityThresholds,
    ValidationThresholds,
)
from balance_pricing_engine.pricing_intelligence.store import ObservationStore


def flagged_market(product):
    market = Market(product)
    market.run(0)
    market.run(1)
    market.set("ASB", 4.60 if product is TD else 4.45)
    market.run(2)
    assert market.only_pending().proposed_rate_pct == (4.60 if product is TD else 4.45)
    return market


def decide(market, event_id, action, when):
    return market.service.review(
        event_id,
        action,
        reviewer="offline-operator",
        reason="Checked synthetic evidence",
        evidence_ref="https://rates.example.test/evidence",
        reviewed_at=when,
    )


@pytest.mark.parametrize("product", [TD, HL])
def test_peer_abstention_and_recovery_never_confirm_pending_reading(product):
    # Removing peer evidence must not upgrade the stored warning or create a new case.
    market = flagged_market(product)
    case = market.only_pending()
    run_id = "run-2" if product is TD else "hl-run-2"
    before = market.snapshot(at(2, 0.5), as_of_run_id=run_id)
    market.fail("ANZ")
    market.fail("BNZ")
    outcome = market.run(3)
    assert any(a.bank == "ASB" for a in outcome.outcome.validation.peer_insufficient_evidence)
    assert market.bank_rate("ASB", at(3, 1)) == (4.00 if product is TD else 5.05)
    repeated = market.only_pending()
    assert (repeated.event_id, repeated.runs_pending) == (case.event_id, 2)
    assert market.ledger_rows() == []
    assert outcome.outcome.changes == ()  # outages never invent withdrawals or reprices
    market.run(3)  # replay is inert
    assert market.only_pending().runs_pending == 2
    market.fail("ANZ", False)
    market.fail("BNZ", False)
    recovered = market.run(4)
    assert recovered.code == 2
    assert market.only_pending().runs_pending == 3
    assert market.only_pending().event_id == case.event_id
    assert market.snapshot(at(2, 0.5), as_of_run_id=run_id) == before
    decide(market, case.event_id, ReviewAction.APPROVE, at(4, 2))
    assert market.bank_rate("ASB", at(4, 2)) == (4.60 if product is TD else 4.45)
    assert market.pending() == []
    market.run(5)
    assert market.ledger_rows()[0]["action"] == "APPROVE"
    notices = [alert for alert in market.sink.alerts if alert.change is None]
    assert len(notices) == 2  # one escalation and one resolution, besides the detection alert


@pytest.mark.parametrize("product", [TD, HL])
def test_activity_before_approval_does_not_change_after_later_decisions(product):
    # A future approval must not clear historical event suspicion, including on VOID.
    market = flagged_market(product)
    case = market.only_pending()
    historical = at(2, 0.5)
    activity = market.service.get_market_activity(now=historical, product_type=product)
    state = market.service.get_market_state(now=historical, product_type=product)
    changes = market.service.get_changes(now=historical, product_type=product)
    approval = at(2, 1)
    decide(market, case.event_id, ReviewAction.APPROVE, approval)
    assert market.service.get_market_activity(now=historical, product_type=product) == activity
    assert market.service.get_market_state(now=historical, product_type=product) == state
    assert market.service.get_changes(now=historical, product_type=product) == changes
    assert (
        market.service.get_market_activity(
            now=approval - timedelta(microseconds=1), product_type=product
        ).changes
        == 0
    )
    approved = market.service.get_market_activity(now=approval, product_type=product)
    assert approved.changes == 1
    decide(market, case.event_id, ReviewAction.VOID, at(2, 2))
    assert market.service.get_market_activity(now=approval, product_type=product) == approved
    assert market.service.get_market_activity(now=historical, product_type=product) == activity
    assert market.service.get_market_activity(now=at(2, 2), product_type=product).changes == 0


@pytest.mark.parametrize("product", [TD, HL])
def test_rejected_peer_reading_stays_untrusted_but_a_different_rate_is_valid(product):
    market = flagged_market(product)
    case = market.only_pending()
    market.fail("ANZ")
    market.fail("BNZ")
    market.run(3)
    decide(market, case.event_id, ReviewAction.REJECT, at(3, 1))
    market.run(4)
    assert market.pending() == []
    assert market.bank_rate("ASB", at(4, 1)) == (4.00 if product is TD else 5.05)
    market.set("ASB", 4.05 if product is TD else 5.00)
    outcome = market.run(5)
    assert market.bank_rate("ASB", at(5, 1)) == (4.05 if product is TD else 5.00)
    assert len(outcome.outcome.changes) == 1
    change = outcome.outcome.changes[0]
    assert (change.previous_rate_pct, change.current_rate_pct, change.change_bps) == (
        (4.00, 4.05, 5.0) if product is TD else (5.05, 5.00, -5.0)
    )
    assert not change.suspicious
    assert [r["action"] for r in market.ledger_rows()] == ["REJECT"]


@pytest.mark.parametrize("product", [TD, HL])
@pytest.mark.parametrize("rebased", [False, True])
@pytest.mark.parametrize("key_change", [False, True])
def test_approval_projects_companions_without_mutating_event_evidence(product, rebased, key_change):
    market = Market(product)
    market.run(0)
    market.set("ASB", 4.90 if product is TD else 6.50)
    if rebased:
        market.run(1)
    market.set(
        "ASB",
        {
            "rate": (4.80 if product is TD else 6.00),
            "promotion_text": "Synthetic promotion",
            "special_conditions": "Synthetic eligibility",
            "max_lvr": 0.75 if key_change else None,
        },
    )
    market.run(2)
    (case,) = [
        c for c in market.pending() if c.proposed_rate_pct == (4.80 if product is TD else 6.00)
    ]
    before = market.store.get_changes_since(now=at(2, 0.5), product_type=product)
    raw = [tuple(row) for row in market.store._connection.execute("SELECT * FROM rate_changes")]
    decide(market, case.event_id, ReviewAction.APPROVE, at(2, 1))
    assert [
        tuple(row) for row in market.store._connection.execute("SELECT * FROM rate_changes")
    ] == raw
    assert market.store.get_changes_since(now=at(2, 0.5), product_type=product) == before
    effective = market.store.get_changes_since(now=at(2, 1), product_type=product)
    target = next(row for row in effective if row["id"] == case.event_id)
    assert (target["previous_rate_pct"], target["current_rate_pct"], target["change_bps"]) == (
        (4.00, 4.80, 80.0) if product is TD else (5.05, 6.00, 95.0)
    )
    assert target["suspicious"] == 0
    companions = [
        row for row in effective if row["run_id"] == case.run_id and row["id"] != case.event_id
    ]
    assert {row["change_type"] for row in companions} == {"PROMOTION_CHANGE", "ELIGIBILITY_CHANGE"}
    assert all(bool(row["suspicious"]) is rebased for row in companions)


@pytest.mark.parametrize("product", [TD, HL])
def test_legacy_cleared_flags_are_still_bounded_by_approval_time(product, tmp_path):
    market = flagged_market(product)
    case = market.only_pending()
    historical = market.service.get_changes(now=at(2, 0.5), product_type=product)
    decide(market, case.event_id, ReviewAction.APPROVE, at(2, 1))
    # Simulate the old Phase 2a implementation's persisted approval, then reopen SQLite.
    record = next(r for r in market.store.get_current_observations(product) if r.bank == "ASB")
    market.store.confirm_suspicious_changes(
        {case.observation_key: (None, record)},
        target_change_ids={case.observation_key: case.event_id},
    )
    path = tmp_path / "legacy-approved.db"
    with sqlite3.connect(path) as copy:
        market.store._connection.backup(copy)
    reopened = ObservationStore(path)
    try:
        old_rows = reopened.get_changes_since(now=at(2, 0.5), product_type=product)
        assert len(old_rows) == 1 and old_rows[0]["suspicious"] == 1
        assert old_rows[0]["change_bps"] == historical[0].change_bps
        assert (
            reopened.get_changes_since(
                now=at(2, 0.5), product_type=product, include_suspicious=False
            )
            == []
        )
        assert (
            len(
                reopened.get_changes_since(
                    now=at(2, 1), product_type=product, include_suspicious=False
                )
            )
            == 1
        )
    finally:
        reopened.close()


@pytest.mark.parametrize("product", [TD, HL])
def test_void_of_an_accepted_reading_preserves_historical_activity(product):
    market = Market(
        product, validation=ValidationThresholds(peer=PeerPlausibilityThresholds(enabled=False))
    )
    market.run(0)
    market.set("ASB", 4.60 if product is TD else 4.45)
    market.run(1)
    event_id = market.store.get_changes_since(product_type=product)[0]["id"]
    historical = market.service.get_market_activity(now=at(1, 0.5), product_type=product)
    assert historical.changes == 1
    decide(market, event_id, ReviewAction.VOID, at(1, 1))
    assert market.service.get_market_activity(now=at(1, 0.5), product_type=product) == historical
    assert market.service.get_market_activity(now=at(1, 1), product_type=product).changes == 0
    assert (
        len(market.store.get_changes_since(now=at(1, 1), product_type=product, include_voided=True))
        == 1
    )


@pytest.mark.parametrize("product", [TD, HL])
def test_store_cannot_insert_a_trusted_duplicate_of_pending_content(product):
    market = flagged_market(product)
    case = market.only_pending()
    record = make_rate(product, "ASB", 12, case.proposed_rate_pct, at(3))
    market.store.record_observations(
        [record], observed_at=at(3), run_id="direct-repeat", product_type=product
    )
    repeated = market.store.get_review_case(case.event_id, now=at(3, 1))
    assert repeated.observation_id == case.observation_id
    assert repeated.runs_pending == 2
    assert market.bank_rate("ASB", at(3, 1)) == (4.00 if product is TD else 5.05)
    assert market.ledger_rows() == []


@pytest.mark.parametrize("product", [TD, HL])
def test_disabling_peer_signal_does_not_confirm_an_existing_quarantine(product):
    market = flagged_market(product)
    case = market.only_pending()
    market.config.validation.peer.enabled = False
    result = market.run(3)
    assert result.outcome.validation.peer_assessments == []
    assert market.only_pending().event_id == case.event_id
    assert market.only_pending().runs_pending == 2
    assert market.bank_rate("ASB", at(3, 1)) == (4.00 if product is TD else 5.05)
    assert any(issue.code == "pending_quarantine" for issue in result.outcome.validation.issues)


@pytest.mark.parametrize("product", [TD, HL])
@pytest.mark.parametrize("eligibility_return", [False, True])
def test_legacy_multihop_approval_preserves_the_full_historical_event(product, eligibility_return):
    market = Market(product)
    market.run(0)
    predecessor = next(
        r for r in market.store.get_last_confirmed_observations(product) if r.bank == "ASB"
    )
    market.set(
        "ASB",
        {
            "rate": 4.90 if product is TD else 6.50,
            "max_lvr": 0.75 if eligibility_return else None,
        },
    )
    market.run(1)
    market.set("ASB", 4.80 if product is TD else 6.00)
    market.run(2)
    (case,) = [
        c for c in market.pending() if c.proposed_rate_pct == (4.80 if product is TD else 6.00)
    ]
    before = market.store.get_changes_since(now=at(2, 0.5), product_type=product)
    decide(market, case.event_id, ReviewAction.APPROVE, at(2, 1))
    record = next(r for r in market.store.get_current_observations(product) if r.bank == "ASB")
    market.store.confirm_suspicious_changes(
        {case.observation_key: (predecessor, record)},
        target_change_ids={case.observation_key: case.event_id},
    )
    assert market.store.get_changes_since(now=at(2, 0.5), product_type=product) == before
