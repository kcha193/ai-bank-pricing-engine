"""Replay of audit challenge case Q (Q6 and Q7) against the preserved term-deposit fixtures.

The original reproductions live in ``docs/audits/2026-11-13-evidence/ingest/case_Q_storage.py``
and are evidence: they are not edited. This replays the same shapes through the same fixture
HTML (same banks, same page markup, same business clock) and asserts the corrected behaviour.

Before (audit, master at 16cb94e):
  Q6  +60 bps misread: accepted unflagged, becomes MARKET_LEADER 4.50, cannot be voided.
  Q7  genuine +100 bps: re-observed daily, never admitted; ASB leaves the market on day 14.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from balance_pricing_engine.competitor_rates.collectors import COLLECTORS_BY_BANK, build_collectors
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.config import (
    PeerPlausibilityThresholds,
    PricingIntelligenceConfig,
    ValidationThresholds,
)
from balance_pricing_engine.pricing_intelligence.health import assess_system_health
from balance_pricing_engine.pricing_intelligence.orchestration import (
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    run_operational_cycle,
)
from balance_pricing_engine.pricing_intelligence.quarantine import ReviewAction, ReviewState
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_intelligence.strategy import resolve_competitor_rate

FIXTURES = Path(__file__).parents[1] / "fixtures" / "pricing_intelligence" / "term_deposits_2026-10-30"
BANKS = ["ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"]
P = ProductType.TERM_DEPOSIT
T0 = datetime(2026, 11, 10, 18, 30, tzinfo=timezone.utc)
ASB_ROW = "<tr><td>12 Months</td><td>4.00% p.a.</td></tr>"
EVIDENCE = "https://rates.example.test/asb/term-deposit-rate-card.pdf"


class FixtureMarket:
    """The audit harness's offline fetcher over the real fixture pages."""

    def __init__(self, tmp_path, **config) -> None:
        self.html = {bank: (FIXTURES / f"{bank.lower()}_term_deposits.html").read_text() for bank in BANKS}
        self.config = PricingIntelligenceConfig(**config)
        self.config = self.config.model_copy(
            update={
                "collection": self.config.collection.model_copy(
                    update={"inter_request_delay_seconds": 0.0, "max_retries": 0}
                )
            }
        )
        self.raw_root = tmp_path / "raw"
        self.store = ObservationStore(":memory:")
        self.service = PricingIntelligenceService(
            config=self.config, store=self.store, collectors=[], raw_root=str(self.raw_root)
        )
        self.set_asb(4.00)

    def set_asb(self, rate: float) -> None:
        pages = dict(self.html)
        pages["ASB"] = self.html["ASB"].replace(ASB_ROW, ASB_ROW.replace("4.00", f"{rate:.2f}"))
        urls = {COLLECTORS_BY_BANK[b].term_deposit_url: pages[b] for b in BANKS}
        self.service._collectors = build_collectors(
            banks=self.config.selected_banks,
            config=self.config.collection,
            fetcher=lambda url: urls[url].encode("utf-8"),
            raw_root=str(self.raw_root),
        )

    def run(self, day: int):
        return run_operational_cycle(
            self.service,
            product_type=P,
            captured_at_utc=T0 + timedelta(days=day),
            run_id=f"run-{day}",
            alert_sinks={},
        )

    def when(self, day: int, hours: float = 1.0) -> datetime:
        return T0 + timedelta(days=day, hours=hours)

    def asb12(self, when):
        market = self.service.get_market_snapshot(product_type=P, now=when).term(12)
        entries = [e for e in (market.entries if market else ()) if e.bank == "ASB"]
        return (entries[0].rate_pct, entries[0].quarantined) if entries else None

    def leader(self, when):
        snapshot = self.service.get_market_snapshot(product_type=P, now=when)
        return resolve_competitor_rate("MARKET_LEADER", snapshot, 12).rate_pct

    def pending(self, day: int):
        return self.service.list_review_cases(P, now=self.when(day))


@pytest.fixture
def market(tmp_path):
    fixture = FixtureMarket(tmp_path)
    fixture.run(0)
    fixture.set_asb(3.90)  # the audit's Q4 recovery: a genuine -10 bps
    fixture.run(1)
    return fixture


# -------------------------------------------------------------------- Q6


def test_t29_q6_a_60bps_misread_is_flagged_pending_and_cannot_lead_the_market(market):
    market.set_asb(4.50)
    result = market.run(2)
    # Before: changes [('ASB 12m: 3.90% -> 4.50% (+60 bps)', suspicious=False)], leader 4.50.
    assert [(c.previous_rate_pct, c.current_rate_pct, c.suspicious) for c in result.outcome.changes] == [
        (3.90, 4.50, True)
    ]
    peer = [a for a in result.outcome.validation.peer_assessments if a.bank == "ASB" and a.status == "flagged"]
    assert len(peer) == 1 and peer[0].peer_count >= 3
    assert market.asb12(market.when(2)) == (3.90, True)  # last trusted rate, marked quarantined
    assert market.leader(market.when(2)) == 4.05
    (case,) = market.pending(2)
    assert case.status is ReviewState.PENDING and case.bank == "ASB"
    assert any("peer_outlier" in signal for signal in case.signals)
    assert result.code == EXIT_OK  # first detection: pending, not yet degraded


def test_t29_q6_rejecting_the_flagged_reading_keeps_it_out_and_ledgers_the_decision(market):
    market.set_asb(4.50)
    market.run(2)
    (case,) = market.pending(2)
    market.service.review(
        case.event_id, ReviewAction.REJECT, reviewer="alice", reason="promo text misread",
        reviewed_at=market.when(2, 2),
    )  # fmt: skip
    market.set_asb(3.90)  # the audit's "correction next day"
    result = market.run(3)
    assert result.outcome.changes == ()  # no fictitious -60 bps reversal event
    assert market.asb12(market.when(3)) == (3.90, False)
    assert market.leader(market.when(3)) == 4.05


def test_t29_q6_an_accepted_misread_can_be_voided_out_of_the_snapshot(tmp_path):
    # Reproduce the audit's *unfixed* acceptance by switching the peer signal off, then void.
    off = ValidationThresholds(peer=PeerPlausibilityThresholds(enabled=False))
    market = FixtureMarket(tmp_path, validation=off)
    market.run(0)
    market.set_asb(3.90)
    market.run(1)
    market.set_asb(4.50)
    result = market.run(2)
    assert [c.suspicious for c in result.outcome.changes] == [False]  # accepted, as in the audit
    assert market.leader(market.when(2)) == 4.50  # the audit's MARKET_LEADER 4.5
    before = market.service.get_market_activity(now=market.when(2, 1), product_type=P)
    assert (before.changes, before.increases) == (2, 1)  # the genuine -10 and the misread +60
    (accepted,) = [
        case
        for case in market.service.list_review_cases(P, [ReviewState.ACCEPTED], now=market.when(2))
        if case.proposed_rate_pct == 4.50
    ]
    market.service.review(
        accepted.event_id, ReviewAction.VOID, reviewer="bob", reason="source parsing error",
        reviewed_at=market.when(2, 2),
    )  # fmt: skip
    assert market.leader(market.when(2, 3)) == 4.05
    assert market.asb12(market.when(2, 3)) == (3.90, True)  # falls back to the valid predecessor
    after = market.service.get_market_activity(now=market.when(2, 3), product_type=P)
    assert (after.changes, after.increases, after.decreases) == (1, 0, 1)  # only the genuine -10
    # Historical evidence is intact: the earlier run still reconstructs what was then trusted.
    assert market.service.get_market_snapshot(
        product_type=P, as_of_run_id="run-2"
    ).term(12) is not None


# -------------------------------------------------------------------- Q7


def test_t30_q7_an_approved_genuine_reprice_becomes_trusted(market):
    market.set_asb(4.90)  # genuine +100 bps
    for day in (2, 3):
        market.run(day)
    assert market.asb12(market.when(3)) == (3.90, True)
    (case,) = market.pending(3)
    market.service.review(
        case.event_id, ReviewAction.APPROVE, reviewer="alice",
        reason="Confirmed on the published rate card", evidence_ref=EVIDENCE,
        reviewed_at=market.when(3, 2),
    )  # fmt: skip
    assert market.asb12(market.when(3, 3)) == (4.90, False)
    assert market.leader(market.when(3, 3)) == 4.90
    result = market.run(4)  # still 4.90 on the page
    assert result.code == EXIT_OK and result.outcome.changes == ()
    # Coherent history: one genuine, un-flagged +100 bps move; the earlier quarantine is kept.
    changes = market.service.get_changes(days=None, now=market.when(4), product_type=P)
    moves = [(c.previous_rate_pct, c.current_rate_pct, c.suspicious) for c in changes if c.is_rate_move]
    assert (3.90, 4.90, False) in moves
    assert len(market.store._connection.execute("SELECT 1 FROM quarantine_reviews").fetchall()) == 1


def test_t30_q7_unapproved_it_stays_untrusted_and_escalates_on_the_nth_run(market):
    market.set_asb(4.90)
    codes = {}
    for day in range(2, 8):
        codes[day] = market.run(day).code
        assert market.asb12(market.when(day)) in ((3.90, True), (3.90, False)), (day, market.asb12(market.when(day)))
        assert market.leader(market.when(day)) == 4.05  # never auto-approved by repetition
    # Default threshold is 3 distinct runs: runs 2, 3, 4 -> degraded from run 4 (exit code 2).
    assert [codes[d] for d in range(2, 8)] == [EXIT_OK, EXIT_OK] + [EXIT_PARTIAL_FAILURE] * 4
    health = assess_system_health(market.service, product_type=P, now=market.when(7))
    assert health.quarantine.escalated and health.quarantine.escalated[0].bank == "ASB"
    assert health.quarantine.escalated[0].runs_pending == 6


def test_t30_q7_the_bank_no_longer_vanishes_unnoticed(market):
    # Audit: by day 14 ASB was absent from the market with exit 0 throughout. Here the same
    # path is loud the whole way, and ASB keeps its last trusted rate while that is fresh.
    market.set_asb(4.90)
    results = [market.run(day) for day in range(2, 12)]
    assert all(r.code == EXIT_PARTIAL_FAILURE for r in results[2:])
    assert all("quarantine" in (r.run_record.error_summary or "").lower() for r in results[2:])
