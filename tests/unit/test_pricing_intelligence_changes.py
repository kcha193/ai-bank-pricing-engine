"""Change detection: real movements become events, quiet days do not."""

from datetime import datetime, timedelta, timezone

from balance_pricing_engine.pricing_intelligence.changes import (
    ChangeType,
    detect_changes,
    load_changes,
)
from balance_pricing_engine.pricing_intelligence.config import ValidationThresholds

DAY_ONE = datetime(2026, 8, 20, tzinfo=timezone.utc)
DAY_TWO = DAY_ONE + timedelta(days=1)


def test_identical_observations_produce_no_changes(make_record):
    today = [make_record(bank="ANZ", rate_pct=4.79), make_record(bank="BNZ", rate_pct=4.85)]
    tomorrow = [
        make_record(bank="ANZ", rate_pct=4.79, observed_at=DAY_TWO),
        make_record(bank="BNZ", rate_pct=4.85, observed_at=DAY_TWO),
    ]

    assert detect_changes(today, tomorrow) == []


def test_rate_decrease_is_detected_with_exact_basis_points(make_record):
    changes = detect_changes(
        [make_record(rate_pct=4.99)],
        [make_record(rate_pct=4.79, observed_at=DAY_TWO)],
    )

    assert len(changes) == 1
    change = changes[0]
    assert change.change_type is ChangeType.RATE_DECREASE
    assert change.previous_rate_pct == 4.99
    assert change.current_rate_pct == 4.79
    assert change.change_bps == -20.0
    assert change.direction == "DOWN"
    assert not change.suspicious


def test_rate_increase_is_detected(make_record):
    changes = detect_changes(
        [make_record(rate_pct=4.79)],
        [make_record(rate_pct=4.89, observed_at=DAY_TWO)],
    )

    assert changes[0].change_type is ChangeType.RATE_INCREASE
    assert changes[0].change_bps == 10.0
    assert changes[0].direction == "UP"


def test_new_product_is_detected(make_record):
    changes = detect_changes(
        [make_record(bank="ANZ")],
        [make_record(bank="ANZ"), make_record(bank="Kiwibank", rate_pct=4.89)],
    )

    assert len(changes) == 1
    assert changes[0].change_type is ChangeType.NEW_PRODUCT
    assert changes[0].bank == "Kiwibank"
    assert changes[0].current_rate_pct == 4.89


def test_removed_product_is_detected(make_record):
    changes = detect_changes(
        [make_record(bank="ANZ"), make_record(bank="ASB", rate_pct=5.05)],
        [make_record(bank="ANZ")],
    )

    assert len(changes) == 1
    assert changes[0].change_type is ChangeType.REMOVED_PRODUCT
    assert changes[0].bank == "ASB"
    assert changes[0].previous_rate_pct == 5.05


def test_removal_is_suppressed_for_a_bank_that_was_not_collected(make_record):
    """An unreachable bank is unknown, not withdrawn."""
    changes = detect_changes(
        previous=[make_record(bank="ANZ"), make_record(bank="Westpac", rate_pct=4.85)],
        current=[make_record(bank="ANZ")],
        banks_collected=["ANZ"],
    )

    assert changes == []


def test_removal_is_still_reported_for_a_bank_that_was_collected(make_record):
    changes = detect_changes(
        previous=[make_record(bank="ANZ"), make_record(bank="ANZ", term_months=60, rate_pct=5.7)],
        current=[make_record(bank="ANZ")],
        banks_collected=["ANZ"],
    )

    assert len(changes) == 1
    assert changes[0].change_type is ChangeType.REMOVED_PRODUCT
    assert changes[0].term_months == 60


def test_oversized_movement_is_flagged_suspicious(make_record):
    changes = detect_changes(
        [make_record(rate_pct=4.89)],
        [make_record(rate_pct=8.49, observed_at=DAY_TWO)],
        thresholds=ValidationThresholds(suspicious_change_bps=75),
    )

    assert changes[0].change_bps == 360.0
    assert changes[0].suspicious
    assert "verify" in changes[0].detail


def test_ordinary_movement_is_not_flagged_suspicious(make_record):
    changes = detect_changes(
        [make_record(rate_pct=4.99)],
        [make_record(rate_pct=4.79, observed_at=DAY_TWO)],
        thresholds=ValidationThresholds(suspicious_change_bps=75),
    )

    assert not changes[0].suspicious


def test_eligibility_change_is_detected_without_a_rate_move(make_record):
    changes = detect_changes(
        [make_record(max_lvr=0.80, min_equity=0.20)],
        [make_record(max_lvr=0.75, min_equity=0.25, observed_at=DAY_TWO)],
    )

    assert [change.change_type for change in changes] == [ChangeType.ELIGIBILITY_CHANGE]
    assert "max_lvr" in changes[0].detail


def test_promotion_change_is_detected(make_record):
    changes = detect_changes(
        [make_record(cashback_text=None)],
        [make_record(cashback_text="$3,000 cashback", observed_at=DAY_TWO)],
    )

    assert [change.change_type for change in changes] == [ChangeType.PROMOTION_CHANGE]


def test_a_reprice_matches_on_identity_rather_than_looking_like_remove_and_add(make_record):
    """A repriced line must be one movement, not a removal plus an addition."""
    changes = detect_changes(
        [make_record(bank="ANZ", term_months=12, rate_pct=4.99)],
        [make_record(bank="ANZ", term_months=12, rate_pct=4.79, observed_at=DAY_TWO)],
    )

    types = [change.change_type for change in changes]
    assert types == [ChangeType.RATE_DECREASE]
    assert ChangeType.NEW_PRODUCT not in types
    assert ChangeType.REMOVED_PRODUCT not in types


def test_a_different_term_is_a_different_product(make_record):
    changes = detect_changes(
        [make_record(term_months=12, rate_pct=4.79)],
        [make_record(term_months=24, rate_pct=4.79)],
    )

    types = {change.change_type for change in changes}
    assert types == {ChangeType.NEW_PRODUCT, ChangeType.REMOVED_PRODUCT}


def test_describe_renders_a_readable_summary(make_record):
    change = detect_changes(
        [make_record(rate_pct=4.99)], [make_record(rate_pct=4.79, observed_at=DAY_TWO)]
    )[0]

    assert change.describe() == "ANZ 12m special: 4.99% -> 4.79% (-20 bps)"


def test_changes_round_trip_through_the_stored_row_shape(make_record):
    change = detect_changes(
        [make_record(rate_pct=4.99)], [make_record(rate_pct=4.79, observed_at=DAY_TWO)]
    )[0]
    row = {
        "observation_key": change.observation_key,
        "bank": change.bank,
        "product_type": "home_loan",
        "product_name": change.product_name,
        "rate_category": change.rate_category,
        "term_months": change.term_months,
        "change_type": change.change_type.value,
        "detected_at": change.detected_at.isoformat(),
        "previous_rate_pct": change.previous_rate_pct,
        "current_rate_pct": change.current_rate_pct,
        "change_bps": change.change_bps,
        "previous_observed_at": None,
        "suspicious": 0,
        "detail": None,
        "source_url": change.source_url,
        "run_id": "run-1",
    }

    restored = load_changes([row])[0]
    assert restored.change_bps == -20.0
    assert restored.change_type is ChangeType.RATE_DECREASE
