"""Deterministic market-state classification."""

from datetime import datetime, timedelta, timezone

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.changes import ChangeType, RateChange
from balance_pricing_engine.pricing_intelligence.config import MarketStateThresholds
from balance_pricing_engine.pricing_intelligence.market_state import (
    MarketState,
    classify_market_state,
)

NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


def make_change(bank, term, previous, current, days_ago=1):
    return RateChange(
        observation_key=f"{bank}|{term}",
        bank=bank,
        product_type=ProductType.HOME_LOAN,
        product_name="Fixed Special",
        rate_category="special",
        term_months=term,
        change_type=(
            ChangeType.RATE_DECREASE if current < previous else ChangeType.RATE_INCREASE
        ),
        detected_at=NOW - timedelta(days=days_ago),
        previous_rate_pct=previous,
        current_rate_pct=current,
        change_bps=rate_change_bps(previous, current),
    )


def test_no_activity_is_low():
    assessment = classify_market_state([], now=NOW)

    assert assessment.state is MarketState.LOW
    assert assessment.changes_7d == 0
    assert "no rate changes" in assessment.rationale


def test_a_single_small_move_is_normal():
    assessment = classify_market_state([make_change("ANZ", 12, 4.99, 4.98)], now=NOW)

    assert assessment.state is MarketState.NORMAL


def test_a_single_large_move_escalates_to_elevated():
    """The first bank of a repricing round matters more than three tiny moves."""
    assessment = classify_market_state([make_change("ANZ", 12, 4.99, 4.74)], now=NOW)

    assert assessment.state is MarketState.ELEVATED
    assert "large-move escalation" in assessment.rationale


def test_moderate_broad_activity_is_elevated():
    changes = [
        make_change("ANZ", 12, 4.99, 4.94),
        make_change("BNZ", 12, 4.95, 4.90),
        make_change("ASB", 24, 5.05, 5.00),
        make_change("ANZ", 24, 5.15, 5.10),
    ]
    assessment = classify_market_state(changes, now=NOW)

    assert assessment.state is MarketState.ELEVATED


def test_broad_heavy_activity_is_high():
    changes = [
        make_change("ANZ", 12, 4.99, 4.79),
        make_change("BNZ", 12, 4.95, 4.85),
        make_change("Westpac", 24, 5.09, 4.99),
        make_change("ASB", 12, 5.05, 4.95),
        make_change("ANZ", 24, 5.15, 5.05),
        make_change("BNZ", 24, 5.10, 5.00),
        make_change("Westpac", 12, 4.99, 4.89),
        make_change("ASB", 24, 5.20, 5.10),
    ]
    assessment = classify_market_state(changes, now=NOW)
    payload = assessment.as_dict()

    assert assessment.state is MarketState.HIGH
    assert payload["changes_7d"] == 8
    assert payload["banks_changed_7d"] == 4
    assert payload["dominant_direction"] == "DOWN"
    assert payload["mean_change_bps"] < 0


def test_one_bank_repricing_its_whole_curve_is_not_a_market_wide_round():
    """Breadth matters: eight changes from one bank is not a HIGH market."""
    changes = [make_change("ANZ", term, 5.0, 4.9) for term in (6, 12, 18, 24, 36, 48, 60)]
    changes.append(make_change("ANZ", 6, 4.9, 4.8, days_ago=2))
    assessment = classify_market_state(changes, now=NOW)

    assert assessment.state is not MarketState.HIGH
    assert assessment.banks_changed_7d == 1


def test_classification_is_deterministic():
    changes = [make_change("ANZ", 12, 4.99, 4.79), make_change("BNZ", 12, 4.95, 4.85)]
    results = {classify_market_state(changes, now=NOW).state for _ in range(10)}

    assert len(results) == 1


def test_thresholds_are_configurable():
    changes = [make_change("ANZ", 12, 4.99, 4.94), make_change("BNZ", 12, 4.95, 4.90)]
    lenient = MarketStateThresholds(
        high_changes=2, high_banks_changed=2, elevated_changes=1, elevated_banks_changed=1
    )

    assert classify_market_state(changes, thresholds=lenient, now=NOW).state is MarketState.HIGH


def test_changes_outside_the_lookback_window_are_ignored():
    old = [make_change("ANZ", 12, 4.99, 4.79, days_ago=30)]

    assert classify_market_state(old, now=NOW).state is MarketState.LOW


def test_assessment_exposes_supporting_metrics():
    payload = classify_market_state([make_change("ANZ", 12, 4.99, 4.79)], now=NOW).as_dict()

    assert set(payload) == {
        "state",
        "changes_7d",
        "banks_changed_7d",
        "terms_changed_7d",
        "mean_change_bps",
        "dominant_direction",
        "rationale",
    }


def test_thresholds_reject_non_monotonic_bands():
    import pytest

    with pytest.raises(ValueError, match="high_changes must be >= elevated_changes"):
        MarketStateThresholds(high_changes=2)

    with pytest.raises(ValueError, match="high_banks_changed must be >= elevated_banks_changed"):
        MarketStateThresholds(high_banks_changed=1)
