"""Historical observation storage and retrieval."""

from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.changes import ChangeType, detect_changes
from balance_pricing_engine.pricing_intelligence.config import ValidationThresholds
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

DAY_ONE = datetime(2026, 8, 20, tzinfo=timezone.utc)
DAY_TWO = DAY_ONE + timedelta(days=1)
DAY_THREE = DAY_ONE + timedelta(days=2)
DAY_FOUR = DAY_ONE + timedelta(days=3)


@pytest.fixture
def store():
    with ObservationStore(":memory:") as observation_store:
        yield observation_store


def test_observations_are_stored_and_read_back(store, make_record):
    store.record_observations([make_record(bank="ANZ", rate_pct=4.79)], observed_at=DAY_ONE)
    latest = store.get_latest_rates()

    assert len(latest) == 1
    assert latest[0].bank == "ANZ"
    assert latest[0].advertised_rate_pct == 4.79


def test_provenance_survives_the_round_trip(store, make_record):
    record = make_record(
        source_url="https://www.anz.co.nz/rates",
        raw_text_context="ANZ 1 year special 4.79%",
        max_lvr=0.80,
        min_equity=0.20,
        cashback_text="$3,000 cashback",
        new_business_only=True,
    )
    store.record_observations([record], observed_at=DAY_ONE)
    restored = store.get_latest_rates()[0]

    assert restored.source_url == "https://www.anz.co.nz/rates"
    assert restored.raw_text_context == "ANZ 1 year special 4.79%"
    assert restored.raw_html_sha256 == record.raw_html_sha256
    assert restored.parser_version == record.parser_version
    assert restored.max_lvr == 0.80
    assert restored.cashback_text == "$3,000 cashback"
    assert restored.new_business_only is True


def test_an_unchanged_re_collection_creates_no_new_state(store, make_record):
    records = [make_record(bank="ANZ", rate_pct=4.79)]
    first = store.record_observations(records, observed_at=DAY_ONE)
    second = store.record_observations(records, observed_at=DAY_TWO)

    assert first.inserted == 1
    assert second.inserted == 0
    assert second.unchanged == 1
    assert len(store.get_rate_history(bank="ANZ")) == 1


def test_an_unchanged_re_collection_still_refreshes_its_provenance(store, make_record):
    """A materially unchanged reading must not leave its stored evidence
    frozen at whatever the *first* observation happened to capture: as
    last_seen_at advances run after run, source_text/content_hash/
    parser_version/source metadata must advance with it too, or an audit of
    "what does the current reading's evidence look like" reads stale
    HTML/context that no longer matches the most recent collection."""
    store.record_observations(
        [
            make_record(
                bank="ANZ",
                rate_pct=4.79,
                raw_text_context="1 year | 4.79% (first snapshot)",
                raw_html_sha256="a" * 64,
                parser_version="v1",
            )
        ],
        observed_at=DAY_ONE,
    )
    result = store.record_observations(
        [
            make_record(
                bank="ANZ",
                rate_pct=4.79,
                raw_text_context="1 year | 4.79% (second snapshot)",
                raw_html_sha256="b" * 64,
                parser_version="v2",
            )
        ],
        observed_at=DAY_TWO,
    )

    assert result.unchanged == 1
    current = store.get_current_observations()
    assert len(current) == 1
    assert current[0].raw_text_context == "1 year | 4.79% (second snapshot)"
    assert current[0].raw_html_sha256 == "b" * 64
    assert current[0].parser_version == "v2"


def test_a_changed_rate_creates_a_new_state(store, make_record):
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
    result = store.record_observations([make_record(rate_pct=4.79)], observed_at=DAY_TWO)

    assert result.inserted == 1
    history = store.get_rate_history(bank="ANZ", term_months=12)
    assert [record.advertised_rate_pct for record in history] == [4.99, 4.79]


def test_write_result_total_sums_inserted_and_unchanged(store, make_record):
    result = store.record_observations(
        [make_record(bank="ANZ"), make_record(bank="BNZ", rate_pct=4.85)], observed_at=DAY_ONE
    )

    assert result.total == result.inserted + result.unchanged == 2


def test_a_numeric_field_becoming_null_is_a_material_change(store, make_record):
    """A stored value disappearing (or appearing) is a change, not noise."""
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20)], observed_at=DAY_ONE
    )
    result = store.record_observations(
        [make_record(max_lvr=None, min_equity=None)], observed_at=DAY_TWO
    )

    assert result.inserted == 1
    assert result.unchanged == 0


def test_a_changed_non_numeric_field_creates_a_new_state(store, make_record):
    store.record_observations([make_record(cashback_text=None)], observed_at=DAY_ONE)
    result = store.record_observations(
        [make_record(cashback_text="$3,000 cashback")], observed_at=DAY_TWO
    )

    assert result.inserted == 1
    assert result.unchanged == 0


def test_a_changed_lvr_condition_creates_a_new_state(store, make_record):
    """lvr_condition must persist a fresh value, not stay stale under 'unchanged'."""
    store.record_observations([make_record(lvr_condition="standard")], observed_at=DAY_ONE)
    result = store.record_observations(
        [make_record(lvr_condition="min_20_percent_equity")], observed_at=DAY_TWO
    )

    assert result.inserted == 1
    assert result.unchanged == 0
    assert store.get_latest_rates()[0].lvr_condition == "min_20_percent_equity"


def test_a_naive_observed_at_is_treated_as_utc(store, make_record):
    naive = datetime(2026, 8, 20, 12, 0, 0)
    store.record_observations([make_record()], observed_at=naive)

    restored = store.get_latest_rates()[0]
    assert restored.captured_at_utc == naive.replace(tzinfo=timezone.utc)


def test_history_is_returned_oldest_first(store, make_record):
    for day, rate in ((DAY_ONE, 4.99), (DAY_TWO, 4.89), (DAY_THREE, 4.79)):
        store.record_observations([make_record(rate_pct=rate)], observed_at=day)

    history = store.get_rate_history(bank="ANZ", term_months=12)
    assert [record.advertised_rate_pct for record in history] == [4.99, 4.89, 4.79]


def test_history_dates_an_unchanged_state_from_its_first_observation(store, make_record):
    """A state's captured_at_utc must not creep forward on every re-observation."""
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_TWO)  # unchanged
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_THREE)  # unchanged

    history = store.get_rate_history(bank="ANZ", term_months=12)

    assert len(history) == 1
    assert history[0].captured_at_utc == DAY_ONE


def test_history_can_be_filtered_by_bank_and_term(store, make_record):
    store.record_observations(
        [
            make_record(bank="ANZ", term_months=12, rate_pct=4.79),
            make_record(bank="ANZ", term_months=24, rate_pct=4.95),
            make_record(bank="BNZ", term_months=12, rate_pct=4.85),
        ],
        observed_at=DAY_ONE,
    )

    assert len(store.get_rate_history(bank="ANZ")) == 2
    assert len(store.get_rate_history(term_months=12)) == 2
    assert len(store.get_rate_history(bank="ANZ", term_months=12)) == 1


def test_history_can_be_filtered_by_since_and_until(store, make_record):
    for day, rate in ((DAY_ONE, 4.99), (DAY_TWO, 4.89), (DAY_THREE, 4.79)):
        store.record_observations([make_record(rate_pct=rate)], observed_at=day)

    assert [r.advertised_rate_pct for r in store.get_rate_history(since=DAY_TWO)] == [
        4.89,
        4.79,
    ]
    assert [r.advertised_rate_pct for r in store.get_rate_history(until=DAY_TWO)] == [
        4.99,
        4.89,
    ]


def test_latest_rates_exclude_a_withdrawn_product(store, make_record):
    store.record_observations(
        [make_record(bank="ANZ"), make_record(bank="ASB", rate_pct=5.05)], observed_at=DAY_ONE
    )
    store.record_observations([make_record(bank="ANZ")], observed_at=DAY_TWO)

    assert {record.bank for record in store.get_latest_rates()} == {"ANZ"}
    assert len(store.get_rate_history()) == 2, "history still holds the withdrawn product"


def test_current_observations_survive_a_run_that_omitted_the_bank(store, make_record):
    """Unlike get_latest_rates, a bank missing from the latest run is not lost."""
    store.record_observations(
        [make_record(bank="ANZ"), make_record(bank="Westpac", rate_pct=4.85)], observed_at=DAY_ONE
    )
    # A run that only collected ANZ (Westpac was unreachable).
    store.record_observations([make_record(bank="ANZ")], observed_at=DAY_TWO)

    assert {record.bank for record in store.get_latest_rates()} == {"ANZ"}
    assert {record.bank for record in store.get_current_observations()} == {"ANZ", "Westpac"}


def test_latest_rates_filter_by_bank_and_term(store, make_record):
    store.record_observations(
        [
            make_record(bank="ANZ", term_months=12, rate_pct=4.79),
            make_record(bank="ANZ", term_months=24, rate_pct=4.95),
            make_record(bank="BNZ", term_months=12, rate_pct=4.85),
        ],
        observed_at=DAY_ONE,
    )

    assert len(store.get_latest_rates(bank="ANZ")) == 2
    assert len(store.get_latest_rates(term_months=12)) == 2


def test_latest_rates_filter_by_rate_category(store, make_record):
    store.record_observations(
        [
            make_record(rate_category="special", rate_pct=4.79),
            make_record(rate_category="standard", rate_pct=5.99),
        ],
        observed_at=DAY_ONE,
    )

    assert [r.rate_category for r in store.get_latest_rates(rate_category="special")] == [
        "special"
    ]


def test_previous_run_rates_are_retrievable(store, make_record):
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
    store.record_observations([make_record(rate_pct=4.79)], observed_at=DAY_TWO)

    previous = store.get_previous_run_rates()
    assert [record.advertised_rate_pct for record in previous] == [4.99]


def test_previous_run_is_empty_on_a_first_collection(store, make_record):
    store.record_observations([make_record()], observed_at=DAY_ONE)

    assert store.get_previous_run_rates() == []


def test_previous_run_is_empty_on_an_entirely_empty_store(store):
    assert store.get_previous_run_rates() == []


def test_previous_run_is_empty_for_an_unknown_run_id(store, make_record):
    store.record_observations([make_record()], observed_at=DAY_ONE)

    assert store.get_previous_run_rates(before_run_id="does-not-exist") == []


def test_previous_run_rates_survive_unchanged_intermediate_collections(store, make_record):
    """An unchanged re-observation must not erase the previous run's membership."""
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_TWO)  # unchanged
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_THREE)  # unchanged

    previous = store.get_previous_run_rates()

    assert [record.advertised_rate_pct for record in previous] == [4.99]


def test_previous_run_rates_include_unchanged_products_after_a_partial_reprice(store, make_record):
    """A reprice on one product must not drop the unchanged ones from the prior view."""
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.99), make_record(bank="BNZ", rate_pct=5.05)],
        observed_at=DAY_ONE,
    )
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.99), make_record(bank="BNZ", rate_pct=5.05)],
        observed_at=DAY_TWO,
    )  # both unchanged
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79), make_record(bank="BNZ", rate_pct=5.05)],
        observed_at=DAY_THREE,
    )  # only ANZ repriced

    previous = store.get_previous_run_rates()

    assert {record.bank: record.advertised_rate_pct for record in previous} == {
        "ANZ": 4.99,
        "BNZ": 5.05,
    }


def test_observations_as_of_reconstructs_a_point_in_time_state(store, make_record):
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
    store.record_observations([make_record(rate_pct=4.79)], observed_at=DAY_TWO)

    as_of_day_one = store.get_observations_as_of(as_of=DAY_ONE)
    as_of_day_two = store.get_observations_as_of(as_of=DAY_TWO)

    assert [record.advertised_rate_pct for record in as_of_day_one] == [4.99]
    assert [record.advertised_rate_pct for record in as_of_day_two] == [4.79]


def test_observations_as_of_dates_records_from_their_first_observation(store, make_record):
    """A later reconfirmation must not put a future timestamp on a past reconstruction."""
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_TWO)  # unchanged

    reconstructed = store.get_observations_as_of(as_of=DAY_ONE)

    assert reconstructed[0].captured_at_utc == DAY_ONE


def test_observations_as_of_does_not_return_evidence_from_a_later_touch(store, make_record):
    """The touch path refreshes source_text/content_hash/parser_version on
    the collapsed row in place, so by the time a state has been reconfirmed
    since day one, the row's evidence columns reflect day two's snapshot --
    not what was actually seen on day one. Returning day two's evidence
    under a day-one reconstruction would be evidence "from the future"
    relative to the reconstructed point, worse than merely stale, so the
    historical read must not surface it as if it were accurate."""
    store.record_observations(
        [make_record(rate_pct=4.99, raw_text_context="day one snapshot")],
        observed_at=DAY_ONE,
    )
    store.record_observations(
        [make_record(rate_pct=4.99, raw_text_context="day two snapshot")],  # unchanged
        observed_at=DAY_TWO,
    )

    reconstructed = store.get_observations_as_of(as_of=DAY_ONE)

    assert reconstructed[0].raw_text_context != "day two snapshot"


def _withdraw_bnz_on_day_two(store, make_record):
    """Shared setup: ANZ+BNZ on day one, BNZ withdrawn (and recorded) on day two."""
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79), make_record(bank="BNZ", rate_pct=4.85)],
        observed_at=DAY_ONE,
    )
    baseline = store.get_current_observations()
    current = [make_record(bank="ANZ", rate_pct=4.79, observed_at=DAY_TWO)]
    store.record_observations(current, observed_at=DAY_TWO)
    changes = detect_changes(
        previous=baseline,
        current=current,
        detected_at=DAY_TWO,
        banks_collected=["ANZ", "BNZ"],
    )
    store.record_changes(changes)


def test_current_observations_exclude_a_confirmed_withdrawal(store, make_record):
    """Unlike a transient collection failure, a recorded withdrawal must not linger."""
    _withdraw_bnz_on_day_two(store, make_record)

    assert {record.bank for record in store.get_current_observations()} == {"ANZ"}


def test_current_observations_exclude_a_superseded_eligibility_state(store, make_record):
    """An old eligibility state must not sit alongside its replacement forever."""
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79)], observed_at=DAY_ONE
    )
    store.record_observations(
        [make_record(max_lvr=0.75, min_equity=0.25, rate_pct=4.99)], observed_at=DAY_TWO
    )

    current = store.get_current_observations()

    assert len(current) == 1
    assert current[0].advertised_rate_pct == 4.99
    assert current[0].max_lvr == 0.75


def test_current_observations_keep_genuinely_simultaneous_lvr_tiers(store, make_record):
    """Two tiers that both keep being reconfirmed are not a transition."""
    both_tiers = [
        make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79),
        make_record(max_lvr=0.70, min_equity=0.30, rate_pct=4.65),
    ]
    store.record_observations(both_tiers, observed_at=DAY_ONE)
    store.record_observations(both_tiers, observed_at=DAY_TWO)  # both reconfirmed unchanged

    current = store.get_current_observations()

    assert len(current) == 2
    assert {record.max_lvr for record in current} == {0.80, 0.70}


def test_no_delayed_withdrawal_follows_an_eligibility_transition(store, make_record):
    """The old key must not resurface as a REMOVED_PRODUCT one collection later."""
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79)], observed_at=DAY_ONE
    )

    baseline_at_run_two = store.get_current_observations()
    transitioned = [
        make_record(max_lvr=0.75, min_equity=0.25, rate_pct=4.79, observed_at=DAY_TWO)
    ]
    store.record_observations(transitioned, observed_at=DAY_TWO)
    changes_at_run_two = detect_changes(
        previous=baseline_at_run_two,
        current=transitioned,
        detected_at=DAY_TWO,
        banks_collected=["ANZ"],
    )
    store.record_changes(changes_at_run_two)
    assert [c.change_type for c in changes_at_run_two] == [ChangeType.ELIGIBILITY_CHANGE]

    baseline_at_run_three = store.get_current_observations()
    reconfirmed = [
        make_record(max_lvr=0.75, min_equity=0.25, rate_pct=4.79, observed_at=DAY_THREE)
    ]
    changes_at_run_three = detect_changes(
        previous=baseline_at_run_three,
        current=reconfirmed,
        detected_at=DAY_THREE,
        banks_collected=["ANZ"],
    )

    assert changes_at_run_three == []


def test_observations_as_of_exclude_a_superseded_eligibility_state(store, make_record):
    """A historical reconstruction must not show both the old and new eligibility
    state as simultaneously current once the transition has happened."""
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79)], observed_at=DAY_ONE
    )
    store.record_observations(
        [make_record(max_lvr=0.75, min_equity=0.25, rate_pct=4.99)], observed_at=DAY_TWO
    )

    before_transition = store.get_observations_as_of(as_of=DAY_ONE)
    after_transition = store.get_observations_as_of(as_of=DAY_TWO)

    assert [r.max_lvr for r in before_transition] == [0.80]
    assert [r.max_lvr for r in after_transition] == [0.75]


def test_observations_as_of_keep_simultaneous_lvr_tiers_that_started_later(store, make_record):
    """A genuinely new simultaneous tier must not exclude an older sibling tier."""
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79)], observed_at=DAY_ONE
    )
    # A second, distinct tier starts later -- not a replacement of the first.
    store.record_observations(
        [
            make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79),  # reconfirmed
            make_record(max_lvr=0.70, min_equity=0.30, rate_pct=4.65),  # new tier
        ],
        observed_at=DAY_THREE,
    )

    current = store.get_observations_as_of(as_of=DAY_THREE)

    assert {r.max_lvr for r in current} == {0.80, 0.70}


def test_confirmed_observations_as_of_falls_back_across_an_eligibility_change(
    store, make_record
):
    """A suspicious value that also shifted eligibility must not lose its fallback."""
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79)], observed_at=DAY_ONE
    )
    warning = make_record(
        max_lvr=0.75, min_equity=0.25, rate_pct=1.50, validation_status="warning"
    )
    store.record_observations([warning], observed_at=DAY_TWO)

    confirmed = store.get_confirmed_observations_as_of(as_of=DAY_TWO)

    assert len(confirmed) == 1
    assert confirmed[0].max_lvr == 0.80
    assert confirmed[0].advertised_rate_pct == 4.79


def test_a_reworded_term_label_does_not_prevent_eligibility_supersession(store, make_record):
    """'1 year' -> '12 months' (same normalised term) must still retire the old row."""
    store.record_observations(
        [
            make_record(
                term_months=12,
                term_label_raw="1 year",
                max_lvr=0.80,
                min_equity=0.20,
                rate_pct=4.79,
            )
        ],
        observed_at=DAY_ONE,
    )
    store.record_observations(
        [
            make_record(
                term_months=12,
                term_label_raw="12 months",
                max_lvr=0.75,
                min_equity=0.25,
                rate_pct=4.99,
            )
        ],
        observed_at=DAY_TWO,
    )

    current = store.get_current_observations()

    assert len(current) == 1
    assert current[0].max_lvr == 0.75


def test_a_reverted_eligibility_condition_starts_a_fresh_state(store, make_record):
    """80% LVR -> 75% -> back to 80% must not merge into the original stale row."""
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=5.19)], observed_at=DAY_ONE
    )
    store.record_observations(
        [make_record(max_lvr=0.75, min_equity=0.25, rate_pct=4.99)], observed_at=DAY_TWO
    )
    # Reverts to the exact same values as the original 80% state.
    result = store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=5.19)], observed_at=DAY_FOUR
    )

    assert result.inserted == 1, "must start a fresh state, not silently touch the old row"

    current = store.get_current_observations()
    assert len(current) == 1
    assert current[0].max_lvr == 0.80
    assert current[0].advertised_rate_pct == 5.19

    # The 75% state, correctly current for the intervening period, must not
    # be resurrected by the later reversion's row now sharing its group.
    mid_period = store.get_observations_as_of(as_of=DAY_THREE)
    assert [r.max_lvr for r in mid_period] == [0.75]


def test_observations_as_of_does_not_resurrect_a_withdrawn_product(store, make_record):
    """A product withdrawn before the cutoff must not be reconstructed as present."""
    _withdraw_bnz_on_day_two(store, make_record)

    reconstructed = store.get_observations_as_of(as_of=DAY_TWO)

    assert {record.bank for record in reconstructed} == {"ANZ"}


def test_previous_run_rates_do_not_resurrect_a_product_withdrawn_in_that_run(store, make_record):
    """The regression this guards: reconstruction must respect recorded withdrawals."""
    _withdraw_bnz_on_day_two(store, make_record)
    # A further, unrelated collection -- the withdrawal is now two runs back.
    store.record_observations([make_record(bank="ANZ", rate_pct=4.79)], observed_at=DAY_THREE)

    previous = store.get_previous_run_rates()

    assert {record.bank for record in previous} == {"ANZ"}


def test_a_product_returning_after_a_withdrawal_starts_a_fresh_state(store, make_record):
    """A re-add is not a continuation: it must not inherit the pre-gap first_observed_at."""
    _withdraw_bnz_on_day_two(store, make_record)
    # BNZ reappears on day three with the exact same rate as before the gap.
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79), make_record(bank="BNZ", rate_pct=4.85)],
        observed_at=DAY_THREE,
    )

    reconstructed = store.get_observations_as_of(as_of=DAY_THREE)
    assert {record.bank for record in reconstructed} == {"ANZ", "BNZ"}

    history = store.get_rate_history(bank="BNZ")
    assert len(history) == 2, "the withdrawal and the return must be distinct states"


def test_a_stale_suspicious_tag_clears_once_reconfirmed(store, make_record):
    """A large move that repeats unchanged is no longer suspicious."""
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
    suspicious = make_record(rate_pct=8.49, observed_at=DAY_TWO, validation_status="warning")
    store.record_observations([suspicious], observed_at=DAY_TWO)
    assert store.get_latest_rates()[0].validation_status == "warning"

    # The same value reappears on day three, validated fresh against itself
    # (no movement), so it is no longer flagged.
    confirmed = make_record(rate_pct=8.49, observed_at=DAY_THREE)
    store.record_observations([confirmed], observed_at=DAY_THREE)

    assert store.get_latest_rates()[0].validation_status != "warning"


def test_last_confirmed_observations_exclude_a_superseded_eligibility_state(store, make_record):
    """The confirmed fallback source needs the same supersession exclusion."""
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79)], observed_at=DAY_ONE
    )
    store.record_observations(
        [make_record(max_lvr=0.75, min_equity=0.25, rate_pct=4.99)], observed_at=DAY_TWO
    )

    confirmed = store.get_last_confirmed_observations()

    assert len(confirmed) == 1
    assert confirmed[0].max_lvr == 0.75


def test_last_confirmed_observations_keep_a_confirmed_state_even_when_superseded_by_a_warning(
    store, make_record
):
    """A warning-tagged sibling must not retire a confirmed predecessor's candidacy."""
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79)], observed_at=DAY_ONE
    )
    store.record_observations(
        [make_record(max_lvr=0.75, min_equity=0.25, rate_pct=4.99)], observed_at=DAY_TWO
    )
    warning = make_record(
        max_lvr=0.80, min_equity=0.20, rate_pct=1.50, validation_status="warning"
    )
    store.record_observations([warning], observed_at=DAY_THREE)

    confirmed = store.get_last_confirmed_observations()

    assert len(confirmed) == 1
    assert confirmed[0].max_lvr == 0.75, "the immediate confirmed predecessor, not the stale key"


def test_a_stale_suspicious_tag_clears_via_a_fresh_state_not_a_mutation(store, make_record):
    """The clearing must not touch the original row's own historical status."""
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
    suspicious = make_record(rate_pct=8.49, observed_at=DAY_TWO, validation_status="warning")
    result = store.record_observations([suspicious], observed_at=DAY_TWO)
    assert result.inserted == 1

    confirmed = make_record(rate_pct=8.49, observed_at=DAY_THREE)
    result = store.record_observations([confirmed], observed_at=DAY_THREE)

    assert result.inserted == 1, "confirmation must start a fresh state, not mutate the old one"
    history = store.get_rate_history(bank="ANZ", term_months=12)
    assert len(history) == 3


def test_confirmation_does_not_rewrite_the_historical_warning_state(store, make_record):
    """A later confirmation must not retroactively change what an earlier run looked like."""
    store.record_observations([make_record(rate_pct=5.00)], observed_at=DAY_ONE)
    warning = make_record(rate_pct=8.00, observed_at=DAY_TWO, validation_status="warning")
    store.record_observations([warning], observed_at=DAY_TWO)
    confirmed = make_record(rate_pct=8.00, observed_at=DAY_THREE)
    store.record_observations([confirmed], observed_at=DAY_THREE)

    as_of_day_two = store.get_confirmed_observations_as_of(as_of=DAY_TWO)

    assert [r.advertised_rate_pct for r in as_of_day_two] == [5.00]


def test_changes_are_persisted_and_queried_by_window(store, make_record):
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
    changes = detect_changes(
        [make_record(rate_pct=4.99)],
        [make_record(rate_pct=4.79, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )
    store.record_changes(changes)

    recent = store.get_changes_since(days=7, now=DAY_THREE)
    assert len(recent) == 1
    assert recent[0]["change_bps"] == -20.0
    assert recent[0]["change_type"] == "RATE_DECREASE"
    assert recent[0]["product_type"] == "home_loan"

    assert store.get_changes_since(days=1, now=DAY_ONE + timedelta(days=10)) == []


def test_changes_since_excludes_events_detected_after_now(store, make_record):
    """A historical now= must not surface a change detected after that point --
    only the lower (since) edge of the window was bounded."""
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
    changes = detect_changes(
        [make_record(rate_pct=4.99)],
        [make_record(rate_pct=4.79, observed_at=DAY_THREE)],
        detected_at=DAY_THREE,
    )
    store.record_changes(changes)

    assert store.get_changes_since(days=7, now=DAY_TWO) == []
    assert len(store.get_changes_since(days=7, now=DAY_THREE)) == 1


def test_recording_the_same_change_twice_is_idempotent(store, make_record):
    changes = detect_changes(
        [make_record(rate_pct=4.99)],
        [make_record(rate_pct=4.79, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )
    store.record_changes(changes)
    store.record_changes(changes)

    assert len(store.get_changes_since(days=7, now=DAY_TWO)) == 1


def test_changes_filter_by_bank_and_term(store, make_record):
    changes = detect_changes(
        [make_record(bank="ANZ", rate_pct=4.99), make_record(bank="BNZ", rate_pct=4.95)],
        [
            make_record(bank="ANZ", rate_pct=4.79, observed_at=DAY_TWO),
            make_record(bank="BNZ", rate_pct=4.85, observed_at=DAY_TWO),
        ],
        detected_at=DAY_TWO,
    )
    store.record_changes(changes)

    assert len(store.get_changes_since(days=7, bank="ANZ", now=DAY_TWO)) == 1
    assert len(store.get_changes_since(days=7, term_months=12, now=DAY_TWO)) == 2
    assert len(store.get_changes_since(days=7, term_months=60, now=DAY_TWO)) == 0


def test_changes_filter_by_product_type(store, make_record):
    home_loan_before = make_record(rate_pct=4.99)
    home_loan_after = make_record(rate_pct=4.79, observed_at=DAY_TWO)
    deposit_before = make_record(
        product_type=ProductType.TERM_DEPOSIT,
        product_name="Term Deposit",
        rate_category="term_deposit",
        rate_pct=3.50,
    )
    deposit_after = make_record(
        product_type=ProductType.TERM_DEPOSIT,
        product_name="Term Deposit",
        rate_category="term_deposit",
        rate_pct=3.60,
        observed_at=DAY_TWO,
    )

    store.record_changes(
        detect_changes(
            [home_loan_before, deposit_before],
            [home_loan_after, deposit_after],
            detected_at=DAY_TWO,
        )
    )

    changes = store.get_changes_since(
        days=7, product_type=ProductType.TERM_DEPOSIT, now=DAY_TWO
    )

    assert len(changes) == 1
    assert changes[0]["product_type"] == ProductType.TERM_DEPOSIT.value


def test_changes_since_can_exclude_suspicious_events(store, make_record):
    changes = detect_changes(
        [make_record(rate_pct=4.89)],
        [make_record(rate_pct=8.49, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
        thresholds=ValidationThresholds(suspicious_change_bps=75),
    )
    store.record_changes(changes)

    assert len(store.get_changes_since(days=7, now=DAY_TWO)) == 1
    assert store.get_changes_since(days=7, now=DAY_TWO, include_suspicious=False) == []


def test_changes_since_preserves_legacy_positional_filters(store, make_record):
    changes = detect_changes(
        [make_record(rate_pct=4.89)],
        [make_record(rate_pct=8.49, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
        thresholds=ValidationThresholds(suspicious_change_bps=75),
    )
    store.record_changes(changes)

    assert store.get_changes_since(None, None, None, None, False, DAY_TWO) == []


def test_banks_and_terms_are_discoverable(store, make_record):
    store.record_observations(
        [
            make_record(bank="ANZ", term_months=12),
            make_record(bank="BNZ", term_months=24, rate_pct=4.99),
        ],
        observed_at=DAY_ONE,
    )

    assert store.get_banks() == ["ANZ", "BNZ"]
    assert store.get_terms() == [12, 24]


def test_an_empty_store_answers_safely(store):
    assert store.get_latest_rates() == []
    assert store.get_rate_history() == []
    assert store.get_changes_since(days=7) == []
    assert store.latest_run_id() is None


def test_atomic_rolls_back_every_write_on_a_later_failure(store, make_record):
    """A failure partway through a group of writes must not leave a partial
    commit -- e.g. an observation persisted with no corresponding change
    event, which a retry of the same collection could never reconstruct
    (it would compare against the already-updated state and see no
    movement)."""
    with pytest.raises(RuntimeError, match="simulated failure"):
        with store.atomic():
            store.record_observations([make_record(rate_pct=4.79)], observed_at=DAY_ONE)
            raise RuntimeError("simulated failure after the observation write")

    assert store.get_current_observations() == []
    assert store.latest_run_id() is None


def test_atomic_commits_every_write_together_on_success(store, make_record):
    changes = detect_changes(
        [make_record(rate_pct=4.99)],
        [make_record(rate_pct=4.79, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )
    with store.atomic():
        store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
        store.record_observations([make_record(rate_pct=4.79)], observed_at=DAY_TWO)
        store.record_changes(changes)

    assert store.get_current_observations() != []
    assert len(store.get_changes_since(days=7, now=DAY_TWO)) == 1


def test_the_store_persists_to_disk(tmp_path, make_record):
    path = tmp_path / "nested" / "observations.db"
    with ObservationStore(path) as store:
        store.record_observations([make_record()], observed_at=DAY_ONE)

    with ObservationStore(path) as reopened:
        assert len(reopened.get_latest_rates()) == 1
