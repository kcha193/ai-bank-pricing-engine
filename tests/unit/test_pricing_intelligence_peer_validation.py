"""DATA-016: the peer-market plausibility signal on validation.

A reading can be under the movement threshold and still wrong. The peer check compares the
candidate with comparable peers and raises a *review* signal; it never rejects or approves.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from quarantine_support import HL, TD, at, make_rate

from balance_pricing_engine.pricing_intelligence.config import (
    PeerPlausibilityThresholds,
    ValidationThresholds,
)
from balance_pricing_engine.pricing_intelligence.validation import validate_observations

NOW = at(5)
PREVIOUS_AT = at(4)


def market(product, rates, *, captured_at, term=12, **extra):
    return [make_rate(product, bank, term, rate, captured_at, **extra) for bank, rate in rates.items()]


TD_BASE = {"ANZ": 4.00, "ASB": 4.00, "BNZ": 4.05, "Westpac": 4.00, "Kiwibank": 4.05}
HL_BASE = {"ANZ": 5.10, "ASB": 5.05, "BNZ": 5.15, "Westpac": 5.10, "Kiwibank": 5.20}


def run(product, base, moves, *, thresholds=None, previous_at=PREVIOUS_AT, drop_previous=(), **extra):
    """Validate a collection where ``moves`` overrides the current rate of some banks."""
    previous = [
        r
        for r in market(product, base, captured_at=previous_at, **extra)
        if r.bank not in drop_previous
    ]
    current_rates = {**base, **moves}
    current = market(product, current_rates, captured_at=NOW, **extra)
    return validate_observations(current, thresholds=thresholds, previous=previous)


def codes(outcome, bank):
    return sorted(i.code for i in outcome.issues if i.bank == bank)


def assessment(outcome, bank):
    (found,) = [a for a in outcome.peer_assessments if a.bank == bank]
    return found


# ------------------------------------------------------------------- T20


def test_t20_a_60bps_misread_with_sufficient_peer_evidence_is_flagged_for_review():
    outcome = run(TD, TD_BASE, {"ASB": 4.60})
    assert codes(outcome, "ASB") == ["peer_outlier"]
    assert any(
        r.bank == "ASB" for r in outcome.accepted
    ), "a peer flag is a review signal: the reading is kept for audit, not rejected"
    assert outcome.rejected == []
    (asb,) = [r for r in outcome.accepted if r.bank == "ASB"]
    assert asb.observation_key() in outcome.suspicious_keys  # will be quarantined
    found = assessment(outcome, "ASB")
    assert found.status == "flagged"
    assert found.peer_count == 4
    assert found.peer_median_pct == pytest.approx(4.025)
    assert found.deviation_bps == pytest.approx(57.5)
    assert found.excess_movement_bps == pytest.approx(60.0)


def test_unflagged_banks_are_untouched_by_one_banks_flag():
    outcome = run(TD, TD_BASE, {"ASB": 4.60})
    for bank in ("ANZ", "BNZ", "Westpac", "Kiwibank"):
        assert codes(outcome, bank) == []


# ------------------------------------------------------------------- T21


def test_t21_insufficient_peer_evidence_does_not_fabricate_a_median():
    sparse = {"ANZ": 4.00, "ASB": 4.00, "BNZ": 4.05}  # only two peers for ASB
    outcome = run(TD, sparse, {"ASB": 4.60})
    assert codes(outcome, "ASB") == []
    found = assessment(outcome, "ASB")
    assert found.status == "insufficient_evidence"
    assert found.peer_count == 2
    assert found.peer_median_pct is None and found.deviation_bps is None
    assert [r.bank for r in outcome.accepted].count("ASB") == 1


def test_peers_without_a_prior_reading_cannot_support_a_movement_judgement():
    outcome = run(TD, TD_BASE, {"ASB": 4.60}, drop_previous=("ANZ", "BNZ", "Westpac"))
    found = assessment(outcome, "ASB")
    assert found.status == "insufficient_evidence"
    assert codes(outcome, "ASB") == []


def test_existing_movement_control_still_applies_when_peer_evidence_is_thin():
    sparse = {"ANZ": 4.00, "ASB": 4.00, "BNZ": 4.05}
    outcome = run(TD, sparse, {"ASB": 4.95})  # +95 bps > 75
    assert codes(outcome, "ASB") == ["suspicious_movement"]


# ------------------------------------------------------------------- T22


def test_t22_genuine_market_wide_movement_is_not_flagged():
    moves = {bank: rate + 0.50 for bank, rate in TD_BASE.items()}
    moves["ASB"] = 4.60  # ASB +60, peers +50: only 10 bps more than the market moved
    outcome = run(TD, TD_BASE, moves)
    assert all(not codes(outcome, bank) for bank in TD_BASE)
    assert outcome.suspicious_keys == set()


def test_t22_a_genuine_competitive_outlier_is_reviewed_not_rejected():
    outcome = run(TD, TD_BASE, {"ASB": 4.60})
    assert outcome.rejected == [] and outcome.errors == []


def test_a_stable_outlier_that_did_not_move_is_not_flagged():
    base = {**TD_BASE, "ASB": 4.70}  # persistently far above peers
    outcome = run(TD, base, {})
    assert outcome.issues == [] and outcome.peer_assessments == []


def test_first_observation_has_no_movement_to_judge():
    current = market(TD, {**TD_BASE, "ASB": 4.80}, captured_at=NOW)
    outcome = validate_observations(current, previous=[])
    assert outcome.issues == []


# ------------------------------------------------------ product direction


def test_deposit_leader_side_is_above_peers_and_loan_leader_side_is_below():
    # +60 above peers: the dangerous side for a deposit (it would win MARKET_LEADER)...
    assert codes(run(TD, TD_BASE, {"ASB": 4.60}), "ASB") == ["peer_outlier"]
    # ...but 60 below peers is the harmless side, which uses the looser bound (75 bps).
    assert codes(run(TD, TD_BASE, {"ASB": 3.40}), "ASB") == []
    assert codes(run(TD, TD_BASE, {"ASB": 3.27}), "ASB") == ["peer_outlier"]  # -73 bps, -75.5 vs peers
    # A mortgage mirrors that: -60 vs peers wins the market (flag), +60 does not.
    assert codes(run(HL, HL_BASE, {"ASB": 4.45}), "ASB") == ["peer_outlier"]
    assert codes(run(HL, HL_BASE, {"ASB": 5.65}), "ASB") == []


# ------------------------------------------------------ comparability / eligibility


def test_peers_at_other_terms_categories_or_balance_tiers_are_not_comparable():
    other_term = market(TD, {"ANZ": 4.00, "BNZ": 4.05, "Westpac": 4.00, "Kiwibank": 4.05}, captured_at=PREVIOUS_AT, term=24)
    asb_prev = market(TD, {"ASB": 4.00}, captured_at=PREVIOUS_AT)
    asb_now = market(TD, {"ASB": 4.60}, captured_at=NOW)
    outcome = validate_observations(asb_now, previous=asb_prev + other_term)
    assert assessment(outcome, "ASB").peer_count == 0

    tiered = market(
        TD, {b: r for b, r in TD_BASE.items() if b != "ASB"}, captured_at=PREVIOUS_AT, min_balance_or_deposit=500_000
    )
    outcome = validate_observations(asb_now, previous=asb_prev + tiered)
    assert assessment(outcome, "ASB").peer_count == 0


def test_stale_peers_are_excluded():
    previous = [make_rate(TD, "ASB", 12, 4.00, NOW - timedelta(hours=1))] + [
        make_rate(TD, bank, 12, rate, NOW - timedelta(days=10))
        for bank, rate in TD_BASE.items()
        if bank != "ASB"
    ]
    current = [make_rate(TD, "ASB", 12, 4.60, NOW)]
    outcome = validate_observations(current, previous=previous)
    assert assessment(outcome, "ASB").status == "insufficient_evidence"
    assert assessment(outcome, "ASB").peer_count == 0


def test_a_peer_that_is_itself_suspicious_this_run_is_not_a_trusted_peer():
    outcome = run(TD, TD_BASE, {"ASB": 4.60, "ANZ": 4.95})  # ANZ +95 is quarantined itself
    assert codes(outcome, "ANZ") == ["suspicious_movement"]
    assert assessment(outcome, "ASB").peer_count == 3  # ANZ no longer counts


def test_the_candidate_bank_is_never_its_own_peer():
    outcome = run(TD, TD_BASE, {"ASB": 4.60})
    assert assessment(outcome, "ASB").peer_count == 4  # five banks, minus ASB


# ----------------------------------------------------------- dispersion / config


def test_a_dispersed_market_raises_the_bar():
    wide = {"ANZ": 3.00, "ASB": 4.00, "BNZ": 3.50, "Westpac": 4.50, "Kiwibank": 5.00}
    outcome = run(TD, wide, {"ASB": 4.60})
    assert codes(outcome, "ASB") == []  # +60 vs a median of 4.0 inside a 100 bps-wide market


def test_peer_check_can_be_disabled_and_thresholds_tuned():
    off = ValidationThresholds(peer=PeerPlausibilityThresholds(enabled=False))
    outcome = run(TD, TD_BASE, {"ASB": 4.60}, thresholds=off)
    assert outcome.issues == [] and outcome.peer_assessments == []
    loose = ValidationThresholds(peer=PeerPlausibilityThresholds(leader_side_deviation_bps=80))
    assert codes(run(TD, TD_BASE, {"ASB": 4.60}, thresholds=loose), "ASB") == []


def test_min_peers_is_validated():
    with pytest.raises(ValueError):
        PeerPlausibilityThresholds(min_peers=1)
