"""Change detection: real movements become events, quiet days do not."""

from datetime import datetime, timedelta, timezone

from balance_pricing_engine.pricing_intelligence.changes import (
    ChangeType,
    detect_changes,
    load_changes,
    resolve_pairing,
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


def test_new_product_is_not_suspicious_by_default(make_record):
    changes = detect_changes(
        [make_record(bank="ANZ")],
        [make_record(bank="ANZ"), make_record(bank="Kiwibank", rate_pct=4.89)],
    )

    new_product = next(c for c in changes if c.change_type is ChangeType.NEW_PRODUCT)
    assert new_product.suspicious is False


def test_a_warning_tagged_new_product_is_flagged_suspicious(make_record):
    """A record already flagged suspicious upstream (e.g. a category flip
    alongside an implausible rate that this method's own resolve_pairing()
    can't match to any predecessor -- rate_category is real identity in
    both observation_key() and eligibility_group_key()) must not surface
    unflagged as a genuine new product in market activity, the dashboard,
    or changes --alerts."""
    changes = detect_changes(
        [make_record(bank="ANZ", rate_category="special", rate_pct=5.00)],
        [
            make_record(
                bank="ANZ",
                rate_category="standard",
                rate_pct=8.00,
                validation_status="warning",
            )
        ],
    )

    new_product = next(c for c in changes if c.change_type is ChangeType.NEW_PRODUCT)
    assert new_product.suspicious is True


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


def test_removal_is_suppressed_for_a_key_rejected_by_validation_this_run(make_record):
    """A record rejected as implausible is unknown, not withdrawn."""
    previous_record = make_record(bank="ANZ")
    changes = detect_changes(
        previous=[previous_record],
        current=[],
        banks_collected=["ANZ"],
        rejected_keys=[previous_record.observation_key()],
    )

    assert changes == []


def test_removal_is_suppressed_for_a_rejected_record_with_a_shifted_eligibility_key(
    make_record,
):
    """A rejected line that also shifted an eligibility field must still
    suppress its true (previous) key, not its own (different) one."""
    baseline = [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79)]
    # The rejected record's own key differs from the baseline's: the
    # eligibility field moved alongside the implausible rate.
    rejected = [make_record(max_lvr=0.75, min_equity=0.25, rate_pct=99.0, observed_at=DAY_TWO)]
    rejected_keys = {
        previous_record.observation_key()
        for previous_record in resolve_pairing(baseline, rejected).values()
    }

    changes = detect_changes(
        previous=baseline,
        current=[],
        detected_at=DAY_TWO,
        banks_collected=["ANZ"],
        rejected_keys=rejected_keys,
    )

    assert changes == []


def test_removal_is_still_reported_for_a_key_not_rejected_this_run(make_record):
    previous_record = make_record(bank="ANZ")
    changes = detect_changes(
        previous=[previous_record],
        current=[],
        banks_collected=["ANZ"],
        rejected_keys=["some-other-key"],
    )

    assert len(changes) == 1
    assert changes[0].change_type is ChangeType.REMOVED_PRODUCT


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


def test_simultaneous_products_with_different_names_are_tracked_independently(make_record):
    """Two products sharing every other field must not collide onto one key."""
    previous = [
        make_record(product_name="Premier", rate_pct=4.79),
        make_record(product_name="Orbit", rate_pct=4.89),
    ]
    current = [
        make_record(product_name="Premier", rate_pct=4.59, observed_at=DAY_TWO),
        make_record(product_name="Orbit", rate_pct=4.89, observed_at=DAY_TWO),
    ]

    changes = detect_changes(previous, current)

    assert len(changes) == 1
    assert changes[0].change_type is ChangeType.RATE_DECREASE
    assert changes[0].product_name == "Premier"


def test_simultaneous_products_with_different_balance_bands_are_tracked_independently(
    make_record,
):
    """Same product name, same coarse fields, different numeric balance tier."""
    previous = [
        make_record(min_balance_or_deposit=1_000, max_balance_or_deposit=50_000, rate_pct=4.79),
        make_record(min_balance_or_deposit=50_000, max_balance_or_deposit=None, rate_pct=4.65),
    ]
    current = [
        make_record(
            min_balance_or_deposit=1_000,
            max_balance_or_deposit=50_000,
            rate_pct=4.59,
            observed_at=DAY_TWO,
        ),
        make_record(
            min_balance_or_deposit=50_000,
            max_balance_or_deposit=None,
            rate_pct=4.65,
            observed_at=DAY_TWO,
        ),
    ]

    changes = detect_changes(previous, current)

    assert len(changes) == 1
    assert changes[0].previous_rate_pct == 4.79
    assert changes[0].current_rate_pct == 4.59


def test_max_lvr_movement_is_an_eligibility_change_not_a_new_product(make_record):
    """LVR eligibility edits are paired to the same product via the fallback match."""
    changes = detect_changes(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79)],
        [make_record(max_lvr=0.75, min_equity=0.25, rate_pct=4.79, observed_at=DAY_TWO)],
    )

    assert [change.change_type for change in changes] == [ChangeType.ELIGIBILITY_CHANGE]


def test_simultaneous_products_with_different_lvr_tiers_are_tracked_independently(make_record):
    """Two products advertised at once, differing only by LVR tier, must not collide."""
    previous = [
        make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79),
        make_record(max_lvr=0.70, min_equity=0.30, rate_pct=4.65),
    ]
    current = [
        make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.59, observed_at=DAY_TWO),
        make_record(max_lvr=0.70, min_equity=0.30, rate_pct=4.65, observed_at=DAY_TWO),
    ]

    changes = detect_changes(previous, current)

    assert len(changes) == 1
    assert changes[0].previous_rate_pct == 4.79
    assert changes[0].current_rate_pct == 4.59


def test_two_simultaneous_eligibility_edits_at_once_fall_back_to_new_and_removed(make_record):
    """An ambiguous pairing (which tier changed to which?) is not guessed at."""
    previous = [
        make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79),
        make_record(max_lvr=0.70, min_equity=0.30, rate_pct=4.65),
    ]
    current = [
        make_record(max_lvr=0.75, min_equity=0.25, rate_pct=4.79, observed_at=DAY_TWO),
        make_record(max_lvr=0.65, min_equity=0.35, rate_pct=4.65, observed_at=DAY_TWO),
    ]

    changes = detect_changes(previous, current)

    types = {change.change_type for change in changes}
    assert types == {ChangeType.NEW_PRODUCT, ChangeType.REMOVED_PRODUCT}
    assert len(changes) == 4


def test_lvr_condition_change_is_an_eligibility_change_not_a_new_product(make_record):
    """A re-described eligibility condition is the same product, not a new one."""
    changes = detect_changes(
        [make_record(lvr_condition="standard")],
        [make_record(lvr_condition="min_20_percent_equity", observed_at=DAY_TWO)],
    )

    assert [change.change_type for change in changes] == [ChangeType.ELIGIBILITY_CHANGE]
    assert "lvr_condition" in changes[0].detail


def test_describe_renders_a_readable_summary(make_record):
    change = detect_changes(
        [make_record(rate_pct=4.99)], [make_record(rate_pct=4.79, observed_at=DAY_TWO)]
    )[0]

    assert change.describe() == "ANZ 12m special: 4.99% -> 4.79% (-20 bps)"


def test_change_type_stringifies_to_its_value():
    assert str(ChangeType.RATE_INCREASE) == "RATE_INCREASE"


def test_describe_renders_new_and_removed_product_summaries(make_record):
    new_change = detect_changes([], [make_record(rate_pct=4.79)])[0]
    assert new_change.describe() == "ANZ 12m special: new at 4.79%"

    removed_change = detect_changes([make_record(rate_pct=4.79)], [])[0]
    assert removed_change.describe() == "ANZ 12m special: withdrawn (was 4.79%)"


def test_describe_falls_back_to_the_change_type_for_non_rate_events(make_record):
    change = detect_changes(
        [make_record(cashback_text=None)],
        [make_record(cashback_text="$3,000 cashback", observed_at=DAY_TWO)],
    )[0]

    assert change.describe() == "ANZ 12m special: promotion_change"


def test_load_changes_tolerates_a_missing_previous_observed_at(make_record):
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
        "run_id": None,
    }

    restored = load_changes([row])[0]
    assert restored.previous_observed_at is None
    assert restored.run_id is None


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
