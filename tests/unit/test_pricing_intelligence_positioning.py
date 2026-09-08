"""Competitive-position and market-activity features."""

from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.changes import ChangeType, RateChange
from balance_pricing_engine.pricing_intelligence.positioning import (
    build_pricing_features,
    compute_competitive_position,
    compute_market_activity,
)
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot

NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)
MARKET = [("ANZ", 4.79), ("BNZ", 4.85), ("Westpac", 4.85), ("ASB", 4.89), ("Kiwibank", 4.89)]


@pytest.fixture
def term_market(make_record):
    records = [make_record(bank=bank, rate_pct=rate) for bank, rate in MARKET]
    return build_market_snapshot(records)[12]


def make_change(
    bank, term, previous, current, days_ago, suspicious=False, rate_category="special"
):
    return RateChange(
        observation_key=f"{bank}|home_loan|{rate_category}|{term}||unknown",
        bank=bank,
        product_type=ProductType.HOME_LOAN,
        product_name="Fixed Special",
        rate_category=rate_category,
        term_months=term,
        change_type=(
            ChangeType.RATE_DECREASE if current < previous else ChangeType.RATE_INCREASE
        ),
        detected_at=NOW - timedelta(days=days_ago),
        previous_rate_pct=previous,
        current_rate_pct=current,
        change_bps=rate_change_bps(previous, current),
        suspicious=suspicious,
    )


def test_position_of_an_uncompetitive_rate(term_market):
    position = compute_competitive_position(term_market, our_rate=4.95)

    assert position.market_best == 4.79
    assert position.market_median == 4.85
    assert position.our_rate_minus_best_bps == 16.0
    assert position.our_rate_minus_median_bps == 10.0
    assert position.market_rank == 6
    assert position.competitors_below_us == 5
    assert position.competitors_above_us == 0
    assert not position.is_market_leader


def test_position_of_a_market_leading_rate(term_market):
    position = compute_competitive_position(term_market, our_rate=4.75)

    assert position.market_rank == 1
    assert position.is_market_leader
    assert position.our_rate_minus_best_bps == -4.0
    assert position.competitors_below_us == 0
    assert position.competitors_above_us == 5


def test_position_counts_matching_rates(term_market):
    position = compute_competitive_position(term_market, our_rate=4.85)

    assert position.competitors_matching_us == 2
    assert position.competitors_below_us == 1
    assert position.market_rank == 2


def test_our_own_bank_is_not_counted_as_a_competitor(term_market):
    position = compute_competitive_position(term_market, our_rate=4.79, our_bank="ANZ")

    assert position.bank_count == 5
    assert position.market_best == 4.85, "ANZ excluded, so BNZ/Westpac lead"
    assert position.market_rank == 1
    # Median/mean/spread over the competitor set [4.85, 4.85, 4.89, 4.89], not
    # the full market including the 4.79 rate being positioned.
    assert position.market_median == 4.87
    assert position.market_mean == 4.87
    assert position.our_rate_minus_median_bps == rate_change_bps(4.87, 4.79)
    assert position.market_spread_bps == 4.0, "4.85-4.89 among competitors, not 4.79-4.89"


def test_our_own_bank_is_excluded_regardless_of_case_or_whitespace(term_market):
    """PricingIntelligenceConfig.our_bank is an arbitrary string, not
    validated against the collectors' canonical names -- "asb" or " ANZ "
    must still exclude the same bank as "ANZ", or a config typo would let
    the tracked bank's own advertised rate silently count as a competitor
    and skew MARKET_MEDIAN/MARKET_LEADER."""
    position = compute_competitive_position(term_market, our_rate=4.79, our_bank=" anz ")

    assert position.bank_count == 5
    assert position.market_best == 4.85, "ANZ excluded, so BNZ/Westpac lead"


def test_position_reports_market_spread(term_market):
    assert compute_competitive_position(term_market, our_rate=4.85).market_spread_bps == 10.0


def test_position_requires_competitors(make_record):
    market = build_market_snapshot([make_record(bank="ANZ", rate_pct=4.79)])[12]

    with pytest.raises(ValueError, match="no competitor rates"):
        compute_competitive_position(market, our_rate=4.79, our_bank="ANZ")


def test_activity_on_a_quiet_market():
    activity = compute_market_activity([], now=NOW)

    assert activity.changes == 0
    assert activity.banks_changed == 0
    assert activity.mean_change_bps == 0.0
    assert activity.dominant_direction == "FLAT"


def test_activity_counts_banks_and_terms():
    changes = [
        make_change("ANZ", 12, 4.99, 4.79, 1),
        make_change("ANZ", 24, 5.15, 5.05, 1),
        make_change("BNZ", 12, 4.95, 4.85, 2),
    ]
    activity = compute_market_activity(changes, now=NOW)

    assert activity.changes == 3
    assert activity.banks_changed == 2
    assert activity.terms_changed == 2
    assert activity.decreases == 3
    assert activity.dominant_direction == "DOWN"


def test_activity_reports_a_mixed_market():
    changes = [
        make_change("ANZ", 12, 4.99, 4.79, 1),
        make_change("BNZ", 12, 4.85, 4.95, 1),
    ]

    assert compute_market_activity(changes, now=NOW).dominant_direction == "MIXED"


def test_activity_excludes_changes_outside_the_window():
    changes = [make_change("ANZ", 12, 4.99, 4.79, 1), make_change("BNZ", 12, 4.95, 4.85, 20)]

    assert compute_market_activity(changes, lookback_days=7, now=NOW).changes == 1
    assert compute_market_activity(changes, lookback_days=30, now=NOW).changes == 2


def test_activity_excludes_changes_detected_after_now():
    """A historical now=... assessment must not count a change detected after
    that point -- only the lookback window's lower edge was bounded, so a
    later-detected change could still leak into an as-of result."""
    changes = [
        make_change("ANZ", 12, 4.99, 4.79, 1),
        make_change("BNZ", 12, 4.95, 4.85, -1),  # detected after `now`
    ]

    assert compute_market_activity(changes, lookback_days=7, now=NOW).changes == 1


def test_activity_excludes_suspicious_changes_by_default():
    """One bogus parser artefact must not fake a repricing round."""
    changes = [
        make_change("ANZ", 12, 4.99, 4.79, 1),
        make_change("Kiwibank", 36, 5.35, 8.49, 1, suspicious=True),
    ]

    default = compute_market_activity(changes, now=NOW)
    raw = compute_market_activity(changes, now=NOW, include_suspicious=True)

    assert default.changes == 1
    assert default.mean_change_bps == -20.0
    assert raw.changes == 2
    assert raw.mean_change_bps > 100


def test_activity_excludes_non_comparable_changes_unconditionally():
    """A green top-up repricing is not a market repricing event and must not
    push the market-state classification -- see BankPricingCollector's
    excluded_product_markers docstring."""
    changes = [
        make_change("ANZ", 12, 4.99, 4.79, 1),
        make_change("ASB", 36, 1.00, 3.50, 1, rate_category="non_comparable"),
    ]

    activity = compute_market_activity(changes, now=NOW, include_suspicious=True)

    assert activity.changes == 1
    assert activity.mean_change_bps == rate_change_bps(4.99, 4.79)


def test_activity_ignores_non_price_events():
    new_product = RateChange(
        observation_key="k",
        bank="ANZ",
        product_type=ProductType.HOME_LOAN,
        product_name="Fixed Special",
        rate_category="special",
        term_months=12,
        change_type=ChangeType.NEW_PRODUCT,
        detected_at=NOW,
        current_rate_pct=4.79,
    )

    assert compute_market_activity([new_product], now=NOW).changes == 0


def test_pricing_features_combine_position_and_activity(term_market):
    changes = [make_change("ANZ", 12, 4.99, 4.79, 1)]
    features = build_pricing_features(term_market, our_rate=4.95, changes_7d=changes,
                                      changes_30d=changes, now=NOW)

    assert features["our_rate_minus_median_bps"] == 10.0
    assert features["changes_7d"] == 1
    assert features["banks_changed_7d"] == 1
    assert features["market_direction_7d"] == "DOWN"
    assert features["changes_30d"] == 1
    assert all(
        isinstance(value, (int, float, str)) for value in features.values()
    ), "features must stay model-ready"
