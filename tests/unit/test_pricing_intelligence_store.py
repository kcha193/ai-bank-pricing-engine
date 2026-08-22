"""Historical observation storage and retrieval."""

from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.pricing_intelligence.changes import detect_changes
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

DAY_ONE = datetime(2026, 8, 20, tzinfo=timezone.utc)
DAY_TWO = DAY_ONE + timedelta(days=1)
DAY_THREE = DAY_ONE + timedelta(days=2)


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


def test_a_changed_rate_creates_a_new_state(store, make_record):
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
    result = store.record_observations([make_record(rate_pct=4.79)], observed_at=DAY_TWO)

    assert result.inserted == 1
    history = store.get_rate_history(bank="ANZ", term_months=12)
    assert [record.advertised_rate_pct for record in history] == [4.99, 4.79]


def test_history_is_returned_oldest_first(store, make_record):
    for day, rate in ((DAY_ONE, 4.99), (DAY_TWO, 4.89), (DAY_THREE, 4.79)):
        store.record_observations([make_record(rate_pct=rate)], observed_at=day)

    history = store.get_rate_history(bank="ANZ", term_months=12)
    assert [record.advertised_rate_pct for record in history] == [4.99, 4.89, 4.79]


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


def test_latest_rates_exclude_a_withdrawn_product(store, make_record):
    store.record_observations(
        [make_record(bank="ANZ"), make_record(bank="ASB", rate_pct=5.05)], observed_at=DAY_ONE
    )
    store.record_observations([make_record(bank="ANZ")], observed_at=DAY_TWO)

    assert {record.bank for record in store.get_latest_rates()} == {"ANZ"}
    assert len(store.get_rate_history()) == 2, "history still holds the withdrawn product"


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


def test_previous_run_rates_are_retrievable(store, make_record):
    store.record_observations([make_record(rate_pct=4.99)], observed_at=DAY_ONE)
    store.record_observations([make_record(rate_pct=4.79)], observed_at=DAY_TWO)

    previous = store.get_previous_run_rates()
    assert [record.advertised_rate_pct for record in previous] == [4.99]


def test_previous_run_is_empty_on_a_first_collection(store, make_record):
    store.record_observations([make_record()], observed_at=DAY_ONE)

    assert store.get_previous_run_rates() == []


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


def test_the_store_persists_to_disk(tmp_path, make_record):
    path = tmp_path / "nested" / "observations.db"
    with ObservationStore(path) as store:
        store.record_observations([make_record()], observed_at=DAY_ONE)

    with ObservationStore(path) as reopened:
        assert len(reopened.get_latest_rates()) == 1
