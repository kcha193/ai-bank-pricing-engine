"""DATA-002: operator review of quarantined readings, and the trust it confers.

Covers the append-only review ledger, the APPROVE / REJECT / VOID state machine,
and how each decision reaches live and historical (as-of) market views.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest
from quarantine_support import HL, TD, Market, at, spike_market

from balance_pricing_engine.pricing_intelligence.config import (
    PeerPlausibilityThresholds,
    ValidationThresholds,
)
from balance_pricing_engine.pricing_intelligence.quarantine import (
    InvalidReviewTransition,
    ReviewAction,
    ReviewInputError,
    ReviewState,
    ReviewTargetNotFound,
)
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

EVIDENCE = "https://rates.example.test/asb/rate-card-2026-11-12.pdf"
REVIEWED_AT = at(2, 1)


def approve(market, event_id, *, reviewed_at=REVIEWED_AT, **overrides):
    kwargs = dict(
        reviewer="alice",
        reason="Verified against the official rate card",
        evidence_ref=EVIDENCE,
        reviewed_at=reviewed_at,
    )
    kwargs.update(overrides)
    return market.service.review(event_id, ReviewAction.APPROVE, **kwargs)


def reject(market, event_id, *, reviewed_at=REVIEWED_AT, **overrides):
    kwargs = dict(reviewer="alice", reason="Promotional text misread", reviewed_at=reviewed_at)
    kwargs.update(overrides)
    return market.service.review(event_id, ReviewAction.REJECT, **kwargs)


def no_peer_check() -> dict:
    """Disable the DATA-016 peer signal so a +60 bps reading is accepted unflagged."""
    return {"validation": ValidationThresholds(peer=PeerPlausibilityThresholds(enabled=False))}


def accepted_spike_market(rate: float = 4.60) -> Market:
    """+60 bps reading accepted (below the 75 bps rule, peer check off): the Q6 shape."""
    return spike_market(day_spike=2, rate=rate, **no_peer_check())


# --------------------------------------------------------------------- queue


def test_t01_suspicious_rate_appears_in_review_queue():
    market = spike_market()
    case = market.only_pending()
    assert (case.bank, case.term_months, case.product_type) == ("ASB", 12, "term_deposit")
    assert (case.previous_rate_pct, case.proposed_rate_pct) == (4.00, 4.90)
    assert case.change_bps == pytest.approx(90.0)
    assert case.status is ReviewState.PENDING
    assert case.run_id and case.source_url and case.runs_pending == 1


def test_queue_is_empty_for_a_clean_market():
    market = Market(TD)
    market.run(0)
    market.run(1)
    assert market.pending() == []


# ---------------------------------------------------------------- validation


def test_t04_approval_without_reason_is_rejected_and_writes_nothing():
    market = spike_market()
    case = market.only_pending()
    for reason in ("", "   "):
        with pytest.raises(ReviewInputError, match="reason"):
            approve(market, case.event_id, reason=reason)
    assert market.ledger_rows() == []


def test_approval_requires_reviewer_and_evidence():
    market = spike_market()
    case = market.only_pending()
    with pytest.raises(ReviewInputError, match="reviewer"):
        approve(market, case.event_id, reviewer=" ")
    with pytest.raises(ReviewInputError, match="evidence"):
        approve(market, case.event_id, evidence_ref=None)
    assert market.ledger_rows() == []


def test_t05_invalid_target_event_is_rejected():
    market = spike_market()
    for bad in (999_999, 0, -3):
        with pytest.raises(ReviewTargetNotFound):
            approve(market, bad)
    assert market.ledger_rows() == []


def test_a_non_rate_event_is_not_a_review_target():
    market = Market(HL)
    market.run(0)
    market.run(1)
    withdrawn = market.store._connection.execute(
        "INSERT INTO rate_changes (observation_key, bank, product_type, product_name, "
        "rate_category, term_months, change_type, detected_at, run_id, suspicious) "
        "VALUES ('k', 'ASB', 'home_loan', 'p', 'special', 12, 'NEW_PRODUCT', ?, 'run-1', 1)",
        (at(1).isoformat(),),
    ).lastrowid
    with pytest.raises(ReviewTargetNotFound):
        approve(market, withdrawn)


# ------------------------------------------------------------------- ledger


def test_t03_approval_is_recorded_append_only_with_full_audit_fields():
    market = spike_market()
    case = market.only_pending()
    outcome = approve(market, case.event_id)
    assert outcome.created is True
    (row,) = market.ledger_rows()
    assert row["action"] == "APPROVE"
    assert (row["previous_state"], row["new_state"]) == ("PENDING", "APPROVED")
    assert (row["bank"], row["product_type"], row["term_months"]) == ("ASB", "term_deposit", 12)
    assert row["rate_category"] == "term_deposit"
    assert row["run_id"] == case.run_id and row["change_id"] == case.event_id
    assert (row["previous_rate_pct"], row["proposed_rate_pct"]) == (4.00, 4.90)
    assert row["reviewer"] == "alice" and row["evidence_ref"] == EVIDENCE
    assert row["reason"] == "Verified against the official rate card"
    assert row["reviewed_at"] == REVIEWED_AT.isoformat()
    assert row["review_id"]


def test_ledger_rows_cannot_be_updated_or_deleted():
    market = spike_market()
    approve(market, market.only_pending().event_id)
    connection = market.store._connection
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("UPDATE quarantine_reviews SET reason = 'edited'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("DELETE FROM quarantine_reviews")
    assert len(market.ledger_rows()) == 1


def test_t06_duplicate_approval_is_idempotent():
    market = spike_market()
    event_id = market.only_pending().event_id
    first = approve(market, event_id)
    second = approve(market, event_id, reviewed_at=at(2, 3))
    assert (first.created, second.created) == (True, False)
    assert second.event.review_id == first.event.review_id
    assert len(market.ledger_rows()) == 1


def test_t07_conflicting_review_actions_are_rejected():
    market = spike_market()
    event_id = market.only_pending().event_id
    approve(market, event_id)
    with pytest.raises(InvalidReviewTransition):
        reject(market, event_id)
    assert [row["action"] for row in market.ledger_rows()] == ["APPROVE"]


def test_void_of_a_pending_reading_and_approve_of_an_accepted_one_are_invalid():
    pending = spike_market()
    with pytest.raises(InvalidReviewTransition, match="PENDING"):
        pending.service.review(
            pending.only_pending().event_id,
            ReviewAction.VOID,
            reviewer="alice",
            reason="x",
            reviewed_at=REVIEWED_AT,
        )
    accepted = accepted_spike_market()
    event_id = accepted.service.list_review_cases(TD, statuses=[ReviewState.ACCEPTED])[0].event_id
    with pytest.raises(InvalidReviewTransition, match="ACCEPTED"):
        approve(accepted, event_id)
    with pytest.raises(InvalidReviewTransition, match="ACCEPTED"):
        reject(accepted, event_id)


def test_rejected_and_voided_are_terminal():
    market = spike_market()
    event_id = market.only_pending().event_id
    reject(market, event_id)
    for action in (ReviewAction.APPROVE, ReviewAction.VOID):
        with pytest.raises(InvalidReviewTransition, match="REJECTED"):
            market.service.review(
                event_id,
                action,
                reviewer="alice",
                reason="again",
                evidence_ref=EVIDENCE,
                reviewed_at=at(2, 5),
            )


def test_t08_concurrent_conflicting_reviews_stay_safe(tmp_path):
    database = tmp_path / "observations.db"
    market = spike_market(store=ObservationStore(database))
    event_id = market.only_pending().event_id
    other = ObservationStore(database)
    results: list[object] = []
    barrier = threading.Barrier(2)

    def attempt(store: ObservationStore, action: ReviewAction) -> None:
        # sqlite3 connections are thread-bound, so each thread opens its own.
        local = ObservationStore(store.database_path)
        barrier.wait()
        try:
            results.append(
                local.apply_review(
                    event_id,
                    action,
                    reviewer="alice",
                    reason="race",
                    evidence_ref=EVIDENCE,
                    reviewed_at=REVIEWED_AT,
                )
            )
        except InvalidReviewTransition as exc:
            results.append(exc)
        finally:
            local.close()

    threads = [
        threading.Thread(target=attempt, args=(other, ReviewAction.APPROVE)),
        threading.Thread(target=attempt, args=(other, ReviewAction.REJECT)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(isinstance(r, InvalidReviewTransition) for r in results) == 1
    assert len(market.ledger_rows()) == 1


# ------------------------------------------------------- approval -> trust


def test_t09_approved_rate_enters_the_trusted_snapshot_and_leader():
    market = spike_market()
    event_id = market.only_pending().event_id
    when = at(2, 2)
    assert market.bank_rate("ASB", when) == 4.00  # quarantined: last trusted rate
    assert market.leader(when) == 4.05
    approve(market, event_id)
    assert market.bank_rate("ASB", when) == 4.90
    assert market.leader(when) == 4.90


def test_t10_approved_change_becomes_eligible_for_market_activity():
    market = spike_market()
    when = at(2, 2)
    assert market.service.get_market_activity(now=when, product_type=TD).changes == 0
    approve(market, market.only_pending().event_id)
    assert market.service.get_market_activity(now=when, product_type=TD).changes == 1
    (change,) = [c for c in market.service.get_changes(now=when, product_type=TD)]
    assert change.suspicious is False
    assert (change.previous_rate_pct, change.current_rate_pct) == (4.00, 4.90)


def test_approval_does_not_alter_unrelated_banks_and_next_run_is_quiet():
    market = spike_market()
    approve(market, market.only_pending().event_id)
    result = market.run(3)
    assert result.outcome.changes == ()  # no duplicate event for the approved move
    assert result.code == 0
    assert market.bank_rate("ANZ", at(3, 1)) == 4.00
    assert market.bank_rate("BNZ", at(3, 1)) == 4.05
    assert market.bank_rate("ASB", at(3, 1)) == 4.90
    assert market.pending() == []


def test_future_legitimate_changes_remain_detectable_after_approval():
    market = spike_market()
    approve(market, market.only_pending().event_id)
    market.run(3)
    market.set("ASB", 4.95)
    result = market.run(4)
    assert [(c.previous_rate_pct, c.current_rate_pct) for c in result.outcome.changes] == [
        (4.90, 4.95)
    ]
    assert result.outcome.changes[0].suspicious is False


# --------------------------------------------------------- rejection -> trust


def test_t11_rejected_rate_remains_excluded_even_when_the_reading_repeats():
    market = spike_market()
    reject(market, market.only_pending().event_id)
    for day in (3, 4, 5):
        result = market.run(day)  # same 4.90 text keeps appearing
        assert result.outcome.changes == ()
    when = at(5, 1)
    assert market.bank_rate("ASB", when) == 4.00  # last trusted rate, still fresh
    assert market.leader(when) == 4.05
    assert market.pending() == []  # a rejected reading does not reopen the queue


def test_t12_rejected_rate_retains_audit_history():
    market = spike_market()
    event_id = market.only_pending().event_id
    reject(market, event_id)
    rows = market.store._connection.execute(
        "SELECT rate_pct, validation_status FROM observations WHERE bank = 'ASB' "
        "AND product_type = 'term_deposit' ORDER BY id"
    ).fetchall()
    assert [(r["rate_pct"], r["validation_status"]) for r in rows] == [
        (4.00, "unvalidated"),
        (4.90, "warning"),
    ]
    (case,) = market.service.list_review_cases(TD, statuses=[ReviewState.REJECTED])
    assert case.event_id == event_id
    (history,) = market.service.get_review_history(event_id)
    assert history.action is ReviewAction.REJECT and history.reason


def test_rejected_then_a_different_genuine_rate_is_judged_afresh():
    market = spike_market()
    reject(market, market.only_pending().event_id)
    market.set("ASB", 4.05)
    result = market.run(3)
    assert [(c.previous_rate_pct, c.current_rate_pct) for c in result.outcome.changes] == [
        (4.00, 4.05)
    ]
    assert market.bank_rate("ASB", at(3, 1)) == 4.05


# ------------------------------------------------------------------- void


def void(market, event_id, *, reviewed_at=at(3, 1), **overrides):
    kwargs = dict(reviewer="bob", reason="Parser error confirmed", reviewed_at=reviewed_at)
    kwargs.update(overrides)
    return market.service.review(event_id, ReviewAction.VOID, **kwargs)


def accepted_event_id(market) -> int:
    (case,) = market.service.list_review_cases(TD, statuses=[ReviewState.ACCEPTED])
    return case.event_id


def test_t13_voided_trusted_rate_no_longer_influences_the_leader():
    market = accepted_spike_market()
    when = at(3, 2)
    assert market.leader(at(2, 2)) == 4.60  # the accepted misread leads the market
    outcome = void(market, accepted_event_id(market))
    assert outcome.event.previous_state is ReviewState.ACCEPTED
    assert outcome.event.new_state is ReviewState.VOIDED
    assert market.leader(when) == 4.05
    assert market.bank_rate("ASB", when) == 4.00


def test_t14_voided_rate_no_longer_influences_the_median():
    market = accepted_spike_market()
    before = market.snapshot(at(2, 2)).term(12).median_rate
    void(market, accepted_event_id(market))
    after = market.snapshot(at(3, 2)).term(12).median_rate
    assert before == pytest.approx(4.05)
    assert after == pytest.approx(4.00)


def test_t15_voided_event_no_longer_influences_market_state():
    market = accepted_spike_market()
    assert market.service.get_market_activity(now=at(3, 2), product_type=TD).changes == 1
    void(market, accepted_event_id(market))
    activity = market.service.get_market_activity(now=at(3, 2), product_type=TD)
    assert activity.changes == 0
    assert market.service.get_changes(now=at(3, 2), product_type=TD) == []


def test_void_without_a_fresh_predecessor_marks_the_rate_unavailable():
    market = Market(TD, **no_peer_check())
    market.run(0)
    market.set("ASB", 4.60)
    market.run(1)
    void(market, accepted_event_id(market), reviewed_at=at(1, 1))
    for day in range(2, 10):
        market.run(day)  # other banks stay fresh; the page keeps showing 4.60
    assert market.bank_rate("ASB", at(2, 1)) == 4.00  # predecessor still inside the window
    assert market.bank_rate("ANZ", at(9, 1)) == 4.00
    assert market.bank_rate("ASB", at(9, 1)) is None  # voided; predecessor now stale


def test_voided_reading_that_keeps_appearing_stays_excluded_without_new_events():
    market = accepted_spike_market()
    void(market, accepted_event_id(market))
    result = market.run(3)  # the page still says 4.60
    assert result.outcome.changes == ()
    assert market.leader(at(3, 2)) == 4.05
    market.set("ASB", 4.00)  # the page is corrected
    result = market.run(4)
    assert result.outcome.changes == ()  # back to the trusted rate: no fictitious reversal
    assert market.bank_rate("ASB", at(4, 1)) == 4.00


# ------------------------------------------------------------ historical views


def test_t16_as_of_views_respect_the_time_of_approval():
    market = spike_market()
    approve(market, market.only_pending().event_id, reviewed_at=at(2, 1))
    market.run(3)
    # run-2 happened before the approval: it must never read as approved.
    assert market.bank_rate("ASB", at(2, 2), as_of_run_id="run-2") == 4.00
    assert market.bank_rate("ASB", at(3, 2), as_of_run_id="run-3") == 4.90
    previous = market.service.get_previous_market_snapshot(product_type=TD)
    assert previous is not None


def test_t16_as_of_views_respect_the_time_of_a_void():
    market = accepted_spike_market()
    void(market, accepted_event_id(market), reviewed_at=at(3, 1))
    market.run(3)
    market.run(4)
    assert market.bank_rate("ASB", at(3, 0), as_of_run_id="run-2") == 4.60
    assert market.bank_rate("ASB", at(3, 0), as_of_run_id="run-3") == 4.60
    assert market.bank_rate("ASB", at(4, 0), as_of_run_id="run-4") == 4.00


# ------------------------------------------------------------- separation


def test_t17_loan_and_deposit_reviews_are_separate():
    deposits = spike_market()
    loans = Market(HL, store=deposits.store)
    loans.run(0)
    loans.run(1)
    loans.set("ASB", 6.30)  # +125 bps
    loans.run(2)
    assert len(deposits.pending()) == 1 and len(loans.pending()) == 1
    assert deposits.only_pending().product_type == "term_deposit"
    assert loans.only_pending().product_type == "home_loan"
    approve(deposits, deposits.only_pending().event_id)
    assert deposits.pending() == [] and len(loans.pending()) == 1
    assert loans.bank_rate("ASB", at(2, 2)) == 5.05  # still the last trusted loan rate


def test_t18_reviewing_one_term_does_not_affect_another():
    market = Market(TD, terms=(12, 24))
    market.run(0)
    market.run(1)
    market.set("ASB", 4.90, term=12)
    market.set("ASB", 4.95, term=24)
    market.run(2)
    cases = {case.term_months: case for case in market.pending()}
    assert set(cases) == {12, 24}
    approve(market, cases[12].event_id)
    assert market.bank_rate("ASB", at(2, 2), term=12) == 4.90
    assert market.bank_rate("ASB", at(2, 2), term=24) == 4.00
    assert [c.term_months for c in market.pending()] == [24]


def test_t19_companion_events_follow_the_rate_event():
    market = Market(HL)
    market.run(0)
    market.run(1)
    market.set("ASB", {"rate": 6.30, "promotion_text": "2% cashback"})
    market.run(2)
    connection = market.store._connection
    flagged = connection.execute(
        "SELECT change_type FROM rate_changes WHERE suspicious = 1 ORDER BY id"
    ).fetchall()
    assert {row["change_type"] for row in flagged} == {"RATE_INCREASE", "PROMOTION_CHANGE"}
    approve(market, market.only_pending().event_id)
    assert connection.execute("SELECT COUNT(*) FROM rate_changes WHERE suspicious = 1").fetchone()[
        0
    ] == 0


# -------------------------------------------------------------- migration


def test_t31_migration_preserves_existing_observations(tmp_path):
    database = tmp_path / "legacy.db"
    market = spike_market(store=ObservationStore(database))
    before = market.store._connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    market.store._connection.execute("DROP TABLE quarantine_reviews")
    market.store._connection.commit()
    market.store.close()
    reopened = ObservationStore(database)  # a pre-2a database gains the ledger additively
    assert reopened._connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == before
    assert reopened._connection.execute("SELECT COUNT(*) FROM quarantine_reviews").fetchone()[0] == 0
    reopened.close()


# ------------------------------------------- exact targeting (no "latest suspicious")


def chain_market() -> Market:
    """Trusted 5.05, then two further quarantined readings of the same identity."""
    market = Market(HL)
    market.run(0)
    market.run(1)
    market.set("ASB", 6.50)  # +145 bps -> quarantined (event 1)
    market.run(2)
    market.set("ASB", 6.00)  # +95 bps against the *trusted* rate -> quarantined again (event 2)
    market.run(3)
    return market


def test_a_replaced_quarantine_is_superseded_and_only_the_current_reading_is_reviewable():
    market = chain_market()
    (current,) = market.pending()
    assert current.proposed_rate_pct == 6.00 and current.runs_pending == 2
    (stale,) = market.service.list_review_cases(HL, [ReviewState.SUPERSEDED], now=market.now)
    assert stale.proposed_rate_pct == 6.50 and stale.event_id != current.event_id
    with pytest.raises(InvalidReviewTransition, match="SUPERSEDED"):
        approve(market, stale.event_id, reviewed_at=at(3, 2))
    assert market.ledger_rows() == []


def test_approving_the_current_reading_rebases_it_onto_the_last_trusted_rate():
    market = chain_market()
    (current,) = market.pending()
    approve(market, current.event_id, reviewed_at=at(3, 2))
    rows = market.store._connection.execute(
        "SELECT previous_rate_pct, current_rate_pct, change_bps, suspicious FROM rate_changes "
        "WHERE id = ?",
        (current.event_id,),
    ).fetchone()
    assert tuple(rows) == (5.05, 6.00, 95.0, 0)  # not the stale 6.50 -> 6.00 hop
    # The superseded event was never approved and stays flagged.
    (stale,) = market.service.list_review_cases(HL, [ReviewState.SUPERSEDED], now=market.now)
    flagged = market.store._connection.execute(
        "SELECT suspicious FROM rate_changes WHERE id = ?", (stale.event_id,)
    ).fetchone()
    assert flagged["suspicious"] == 1
    assert market.bank_rate("ASB", at(3, 3)) == 6.00


def test_confirm_suspicious_changes_can_be_pinned_to_one_exact_event():
    market = chain_market()
    connection = market.store._connection
    ids = [r["id"] for r in connection.execute(
        "SELECT id FROM rate_changes WHERE suspicious = 1 AND change_type IN "
        "('RATE_INCREASE', 'RATE_DECREASE') ORDER BY id"
    )]  # fmt: skip
    assert len(ids) == 2
    key = connection.execute("SELECT observation_key FROM rate_changes WHERE id = ?", (ids[0],)).fetchone()[0]
    record = market.store.get_current_observations(HL)[0]
    market.store.confirm_suspicious_changes({key: (None, record)}, target_change_ids={key: ids[0]})
    remaining = [r["id"] for r in connection.execute("SELECT id FROM rate_changes WHERE suspicious = 1")]
    assert remaining == [ids[1]]  # exactly the named event, not "the latest"


def test_unreadable_stored_signals_do_not_break_the_queue():
    market = spike_market()
    market.store._connection.execute(
        "UPDATE observations SET validation_warnings = 'not json' WHERE validation_status = 'warning'"
    )
    (case,) = market.pending()
    assert case.signals == ()


def test_a_dry_run_validates_without_writing():
    market = spike_market()
    event_id = market.only_pending().event_id
    outcome = approve(market, event_id, dry_run=True)
    assert outcome.dry_run is True and outcome.event is None and outcome.created is False
    assert market.ledger_rows() == []
    with pytest.raises(InvalidReviewTransition):
        market.service.review(
            event_id, ReviewAction.VOID, reviewer="a", reason="x", reviewed_at=REVIEWED_AT, dry_run=True
        )


def test_review_cannot_predate_the_event_or_an_earlier_decision():
    market = spike_market()
    event_id = market.only_pending().event_id
    with pytest.raises(ReviewInputError, match="precedes"):
        approve(market, event_id, reviewed_at=at(1))
    assert market.ledger_rows() == []


def test_oversized_or_control_character_fields_are_refused():
    market = spike_market()
    event_id = market.only_pending().event_id
    for bad in ("x" * 1001, "line\x00break", "esc\x1b[31m"):
        with pytest.raises(ReviewInputError):
            approve(market, event_id, reason=bad)
