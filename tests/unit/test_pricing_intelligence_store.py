"""Historical observation storage and retrieval."""

from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.changes import ChangeType, RateChange, detect_changes
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


def _reuse_run_id_for_another_product(store, run_id, records, observed_at, product_type):
    """Simulate ``run_id`` being reused for a second product's call, the way
    a pre-guard version of ``record_observations()`` used to allow --
    something the current API now refuses outright (see
    ``test_reusing_a_run_id_with_a_different_observed_at_is_rejected``).
    Writes normally under a throwaway run_id, then relabels every row that
    call touched onto the shared ``run_id`` and folds its ``collection_runs``
    metadata into the shared row, reproducing exactly the database state the
    old, unguarded code path used to leave behind -- e.g. for tests
    exercising how the read side recovers a genuinely mixed legacy run."""
    throwaway_run_id = f"{run_id}::throwaway"
    store.record_observations(
        records, run_id=throwaway_run_id, observed_at=observed_at, product_type=product_type
    )
    connection = store._connection
    throwaway = connection.execute(
        "SELECT product_type, banks, record_count FROM collection_runs WHERE run_id = ?",
        (throwaway_run_id,),
    ).fetchone()
    connection.execute(
        "UPDATE collection_runs SET product_type = ?, banks = ?, record_count = ? WHERE run_id = ?",
        (throwaway["product_type"], throwaway["banks"], throwaway["record_count"], run_id),
    )
    connection.execute(
        "UPDATE observations SET last_seen_run_id = ? WHERE last_seen_run_id = ?",
        (run_id, throwaway_run_id),
    )
    connection.execute(
        "UPDATE observations SET first_seen_run_id = ? WHERE first_seen_run_id = ?",
        (run_id, throwaway_run_id),
    )
    connection.execute(
        "UPDATE rate_changes SET run_id = ? WHERE run_id = ?", (run_id, throwaway_run_id)
    )
    connection.execute(
        "UPDATE collection_run_products SET run_id = ? WHERE run_id = ?",
        (run_id, throwaway_run_id),
    )
    connection.execute("DELETE FROM collection_runs WHERE run_id = ?", (throwaway_run_id,))


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


def test_record_observations_rejects_a_mixed_product_batch(store, make_record):
    """One run must be a single product's collection: collection_runs.product_type
    cannot represent more than one product for a single run_id, so a caller
    passing a mixed home-loan/deposit batch in one call would otherwise
    silently mislabel the whole run under just the first record's product --
    making latest_run_id()/get_latest_rates() blind to the other product's
    freshly-stored rows even though they were saved correctly."""
    mixed = [
        make_record(product_type=ProductType.HOME_LOAN),
        make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit"),
    ]

    with pytest.raises(ValueError, match="mixed-product batch"):
        store.record_observations(mixed, observed_at=DAY_ONE)


def test_record_observations_preserves_an_explicit_product_type_for_an_empty_run(store):
    """Inferring collection_runs.product_type solely from an empty records
    list silently defaults to HOME_LOAN regardless of what was actually being
    collected -- e.g. a term-deposit run where every bank withdrew, or a
    failed attempt worth recording. A caller that already knows the product
    (PricingIntelligenceService.collect() always does) must be able to say so
    explicitly instead of relying on that inference."""
    store.record_observations(
        [], run_id="empty-deposit-run", observed_at=DAY_ONE, product_type=ProductType.TERM_DEPOSIT
    )

    assert store.latest_run_id(product_type=ProductType.TERM_DEPOSIT) == "empty-deposit-run"
    assert store.latest_run_id(product_type=ProductType.HOME_LOAN) is None


def test_record_observations_rejects_a_product_type_that_contradicts_its_records(
    store, make_record
):
    """An explicit product_type is a caller assertion, not an override -- it
    must agree with what the records themselves say, or a genuine mismatch
    (a caller passing the wrong product) would otherwise be silently trusted
    over the records' own truth."""
    home_loan_record = make_record(product_type=ProductType.HOME_LOAN)

    with pytest.raises(ValueError, match="term_deposit"):
        store.record_observations(
            [home_loan_record], observed_at=DAY_ONE, product_type=ProductType.TERM_DEPOSIT
        )


def test_retrying_a_run_id_does_not_reorder_it_after_a_later_run(store, make_record):
    """INSERT OR REPLACE deletes and reinserts a collection_runs row on a
    run_id collision, handing it a fresh (higher) rowid --
    _run_ordered_after()/_as_of_cutoff() use that rowid to break a
    started_at tie. Retrying an earlier run under its own run_id after a
    later same-timestamp run has already committed must not let that retry
    jump ahead of the later run in rowid order, or latest_run_id() and every
    run-anchored reconstruction would order the two runs backwards."""
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79)], run_id="run-earlier", observed_at=DAY_TWO
    )
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.89)], run_id="run-later", observed_at=DAY_TWO
    )

    # Retry "run-earlier" (e.g. an idempotent re-processing keyed by run_id)
    # -- must not disturb its position relative to "run-later".
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79)], run_id="run-earlier", observed_at=DAY_TWO
    )

    assert store.latest_run_id() == "run-later"


def test_retrying_a_run_id_does_not_replay_its_observations(store, make_record):
    """Preserving collection_runs' row position on a retried run_id (see
    test_retrying_a_run_id_does_not_reorder_it_after_a_later_run) is not
    enough on its own: replaying the same records through
    _upsert_observation() would still drag last_seen_run_id on an unchanged
    row backwards to the retried (older) run, even though a genuinely later
    run has since confirmed it. get_latest_rates() filters on an exact
    last_seen_run_id match against latest_run_id(), so a naive replay would
    leave latest_run_id() correctly pointing at the later run while
    get_latest_rates() finds no row at all -- the exact scenario this
    idempotent short-circuit exists to prevent."""
    anz_record = make_record(bank="ANZ", rate_pct=4.79)
    original = store.record_observations([anz_record], run_id="run-earlier", observed_at=DAY_TWO)
    # The rate is unchanged, so this later run only refreshes last_seen_run_id.
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79, observed_at=DAY_TWO)],
        run_id="run-later",
        observed_at=DAY_TWO,
    )

    retried = store.record_observations(
        [anz_record], run_id="run-earlier", observed_at=DAY_TWO
    )

    assert store.latest_run_id() == "run-later"
    assert [record.bank for record in store.get_latest_rates()] == ["ANZ"]
    assert retried == original, "a retry must report the same outcome as the original call"


def test_retrying_one_side_of_a_mixed_legacy_run_does_not_replay_its_observations(
    store, make_record
):
    """collection_runs.product_type can only hold one value, so a legacy
    run_id that recorded both products (see test_a_legacy_run_that_mixed_
    both_products_stays_findable_under_either) has that column pinned to
    whichever product's call happened last -- home_loan gets overwritten to
    term_deposit here. Retrying the now-overwritten home_loan side must
    still be recognised as a repeat and short-circuited, not just when the
    stored column happens to match: recognising it only via that lossy
    column would replay the home-loan record straight through, dragging
    last_seen_run_id back to the shared legacy run_id even though a later,
    genuinely newer home-loan run has since confirmed it."""
    shared_run_id = "legacy-mixed-run"
    home_loan_record = make_record(bank="ANZ", product_type=ProductType.HOME_LOAN, rate_pct=4.79)
    deposit_record = make_record(
        bank="ANZ", product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit"
    )
    store.record_observations([home_loan_record], run_id=shared_run_id, observed_at=DAY_ONE)
    # Overwrites collection_runs.product_type to term_deposit -- the
    # home-loan side's own stored counts are lost from this point on.
    _reuse_run_id_for_another_product(
        store, shared_run_id, [deposit_record], DAY_ONE, ProductType.TERM_DEPOSIT
    )

    # A later, genuine home-loan run reconfirms the same (unchanged) rate.
    store.record_observations(
        [make_record(bank="ANZ", product_type=ProductType.HOME_LOAN, rate_pct=4.79)],
        run_id="run-later-home-loan",
        observed_at=DAY_TWO,
    )

    # Retry the shared run's home-loan side.
    store.record_observations([home_loan_record], run_id=shared_run_id, observed_at=DAY_ONE)

    assert store.latest_run_id(product_type=ProductType.HOME_LOAN) == "run-later-home-loan"
    assert [record.bank for record in store.get_latest_rates(product_type=ProductType.HOME_LOAN)] == [
        "ANZ"
    ]


def test_retrying_a_quiet_mixed_legacy_run_side_does_not_replay_its_observations(
    store, make_record
):
    """The gap a durable collection_run_products ledger exists to close: a
    legacy mixed run whose home-loan side merely reconfirmed a pre-existing
    row (producing no rate_changes, and leaving first_seen_run_id pointing
    at the original bootstrap run, not this shared one) leaves *no*
    observation/rate_changes evidence at all once a later home-loan run
    moves last_seen_run_id on -- and collection_runs.product_type gets
    overwritten to term_deposit by the mixed run's other side. Every
    fallback signal the previous fix relied on is therefore false; only a
    durable, write-once per-(run_id, product_type) record still recognises
    a retry of that shared run's home-loan side."""
    # The bootstrap run that first introduces ANZ's rate -- distinct from
    # the shared mixed run below, so first_seen_run_id never points to it.
    store.record_observations(
        [make_record(bank="ANZ", product_type=ProductType.HOME_LOAN, rate_pct=4.79)],
        run_id="bootstrap",
        observed_at=DAY_ONE,
    )

    shared_run_id = "legacy-mixed-quiet-run"
    # Reconfirms the bootstrap rate unchanged -- produces no rate_changes
    # and does not touch first_seen_run_id.
    store.record_observations(
        [make_record(bank="ANZ", product_type=ProductType.HOME_LOAN, rate_pct=4.79)],
        run_id=shared_run_id,
        observed_at=DAY_TWO,
    )
    # Overwrites collection_runs.product_type to term_deposit.
    _reuse_run_id_for_another_product(
        store,
        shared_run_id,
        [make_record(bank="ANZ", product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")],
        DAY_TWO,
        ProductType.TERM_DEPOSIT,
    )

    # A later, genuine home-loan run reconfirms the same rate again, moving
    # last_seen_run_id on from the shared run.
    store.record_observations(
        [make_record(bank="ANZ", product_type=ProductType.HOME_LOAN, rate_pct=4.79)],
        run_id="run-later-home-loan",
        observed_at=DAY_THREE,
    )

    # Retry the shared run's home-loan side.
    store.record_observations(
        [make_record(bank="ANZ", product_type=ProductType.HOME_LOAN, rate_pct=4.79)],
        run_id=shared_run_id,
        observed_at=DAY_TWO,
    )

    assert store.latest_run_id(product_type=ProductType.HOME_LOAN) == "run-later-home-loan"
    assert [record.bank for record in store.get_latest_rates(product_type=ProductType.HOME_LOAN)] == [
        "ANZ"
    ]


def test_migrating_collection_run_products_backfills_from_surviving_evidence(store, make_record):
    """A database created before collection_run_products existed must have
    it backfilled from whatever evidence still survives -- collection_runs'
    own stored product_type/started_at paired with its recorded
    inserted/unchanged counts (see _migrate_collection_run_products())."""
    write = store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79)], run_id="run-one", observed_at=DAY_ONE
    )

    # Simulate a database that predates the table.
    store._connection.execute("DROP TABLE collection_run_products")
    store._create_schema()

    row = store._connection.execute(
        "SELECT started_at, inserted_count, unchanged_count FROM collection_run_products "
        "WHERE run_id = ? AND product_type = ?",
        ("run-one", ProductType.HOME_LOAN.value),
    ).fetchone()
    assert row is not None
    assert (row["inserted_count"], row["unchanged_count"]) == (write.inserted, write.unchanged)
    assert row["started_at"] == DAY_ONE.isoformat()

    # Re-running the migration must be a no-op, not a duplicate/overwrite.
    store._create_schema()
    rows = store._connection.execute(
        "SELECT COUNT(*) AS n FROM collection_run_products WHERE run_id = ?", ("run-one",)
    ).fetchone()
    assert rows["n"] == 1


def test_migrating_collection_run_products_adds_started_at_to_a_pre_column_table(
    store, make_record
):
    """A database that already has collection_run_products from this
    store's previous revision (before started_at joined it) must have that
    column added and backfilled, not left on the table's own DEFAULT ''
    placeholder -- which would otherwise make every one of its rows sort as
    the earliest possible run in latest_run_id()/_previous_run_row()'s
    ordering."""
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79)], run_id="run-one", observed_at=DAY_ONE
    )

    # Simulate a database whose collection_run_products predates started_at.
    store._connection.executescript(
        "DROP TABLE collection_run_products;"
        "CREATE TABLE collection_run_products ("
        "    run_id TEXT NOT NULL, product_type TEXT NOT NULL, "
        "    inserted_count INTEGER NOT NULL DEFAULT 0, "
        "    unchanged_count INTEGER NOT NULL DEFAULT 0, "
        "    PRIMARY KEY (run_id, product_type)"
        ");"
        "INSERT INTO collection_run_products (run_id, product_type, inserted_count, unchanged_count) "
        "VALUES ('run-one', 'home_loan', 1, 0);"
    )
    store._create_schema()

    row = store._connection.execute(
        "SELECT started_at FROM collection_run_products WHERE run_id = ? AND product_type = ?",
        ("run-one", ProductType.HOME_LOAN.value),
    ).fetchone()
    assert row["started_at"] == DAY_ONE.isoformat()


def test_get_previous_run_started_at_finds_a_quiet_mixed_run_that_lost_its_evidence(
    store, make_record
):
    """Direct-read run selection is exposed to the same evidence-loss gap as
    record_observations()'s own idempotency check: a legacy run_id that
    recorded both products (one call per product) with its home-loan side
    merely reconfirming a pre-existing row (no rate_changes) has that
    reconfirmation's only trace -- last_seen_run_id -- erased the moment a
    later home-loan run touches the same key, while
    collection_runs.product_type has already been overwritten by the run's
    own deposit side. get_previous_run_started_at(HOME_LOAN) must still find
    that quiet mixed run as the immediately-preceding home-loan run via
    collection_run_products, not skip straight past it to an even earlier
    one."""
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79)], run_id="bootstrap", observed_at=DAY_ONE
    )

    shared_run_id = "legacy-mixed-quiet-run"
    # Reconfirms the bootstrap rate unchanged -- produces no rate_changes
    # and does not touch first_seen_run_id.
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79)], run_id=shared_run_id, observed_at=DAY_TWO
    )
    # Overwrites collection_runs.product_type to term_deposit.
    _reuse_run_id_for_another_product(
        store,
        shared_run_id,
        [make_record(bank="ANZ", product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")],
        DAY_TWO,
        ProductType.TERM_DEPOSIT,
    )

    # A later, genuine home-loan run reconfirms the same rate again, moving
    # last_seen_run_id on from the shared run -- erasing its last remaining
    # trace under the old fallback-only matching.
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79)],
        run_id="run-later-home-loan",
        observed_at=DAY_THREE,
    )

    assert store.get_previous_run_started_at(product_type=ProductType.HOME_LOAN) == DAY_TWO


def test_reusing_a_run_id_for_a_different_product_is_rejected(store, make_record):
    """record_observations()'s upsert replaces every collection_runs column
    on a run_id collision, including started_at, and preserves the run's one
    rowid across it -- reusing a run_id for a second product would either
    retroactively overwrite the first product's recorded start time (a
    different observed_at) or, even at a matching observed_at, leave the
    run_id's one fixed rowid unable to represent both products' correct
    position relative to some third run that committed in between the two
    calls. Every ordering query that reads collection_runs.started_at/rowid
    directly (_run_ordered_after(), _as_of_cutoff(), get_run_started_at(),
    and more) is exposed to either failure mode. Rejecting cross-product
    reuse outright closes this off entirely, rather than requiring every
    past and future ordering query to defend against it individually. A
    pre-existing (legacy) database can still hold a genuinely mixed run_id
    from before this guard existed -- see test_a_legacy_run_that_mixed_
    both_products_stays_findable_under_either for how the read side
    recovers that."""
    shared_run_id = "shared"
    store.record_observations(
        [make_record(bank="ANZ", product_type=ProductType.HOME_LOAN, rate_pct=4.79)],
        run_id=shared_run_id,
        observed_at=DAY_ONE,
    )

    with pytest.raises(ValueError, match="already recorded"):
        store.record_observations(
            [
                make_record(
                    bank="ANZ", product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit"
                )
            ],
            run_id=shared_run_id,
            observed_at=DAY_THREE,
        )

    # Even a matching observed_at is refused: collection_runs' rowid, fixed
    # at whichever position the first call established, still can't
    # represent this run_id's second product correctly against a third run
    # sharing that same timestamp and committing in between.
    with pytest.raises(ValueError, match="already recorded"):
        store.record_observations(
            [
                make_record(
                    bank="ANZ", product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit"
                )
            ],
            run_id=shared_run_id,
            observed_at=DAY_ONE,
        )


def test_latest_run_id_is_scoped_per_product(store, make_record):
    """Home-loan and term-deposit collections share one collection_runs
    table, so an unscoped latest_run_id() can return either product's run --
    it must resolve "the latest run for this market" instead."""
    store.record_observations(
        [make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")],
        observed_at=DAY_ONE,
    )
    store.record_observations([make_record(product_type=ProductType.HOME_LOAN)], observed_at=DAY_TWO)

    deposit_run = store.latest_run_id(product_type=ProductType.TERM_DEPOSIT)
    loan_run = store.latest_run_id(product_type=ProductType.HOME_LOAN)

    assert deposit_run != loan_run
    assert store.get_run_started_at(deposit_run) == DAY_ONE
    assert store.get_run_started_at(loan_run) == DAY_TWO


def test_previous_market_snapshot_is_not_confused_by_the_other_products_run(store, make_record):
    """The exact scenario a shared collection_runs table introduces: deposit
    run D1 followed by a mortgage run M1 must not make
    get_previous_run_started_at(product_type=TERM_DEPOSIT) resolve to D1's
    own (current) state -- D1 is the only deposit run so far, so there is no
    previous deposit snapshot yet."""
    store.record_observations(
        [make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")],
        observed_at=DAY_ONE,
    )
    store.record_observations([make_record(product_type=ProductType.HOME_LOAN)], observed_at=DAY_TWO)

    assert store.get_previous_run_started_at(product_type=ProductType.TERM_DEPOSIT) is None

    # A second, genuinely later deposit run must now correctly resolve D1 as
    # its predecessor, not the intervening mortgage run.
    store.record_observations(
        [
            make_record(
                product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit", rate_pct=4.20
            )
        ],
        observed_at=DAY_THREE,
    )

    assert store.get_previous_run_started_at(product_type=ProductType.TERM_DEPOSIT) == DAY_ONE


def test_a_database_with_no_surviving_trace_of_a_run_falls_back_to_home_loan(tmp_path):
    """A database file created before collection_runs.product_type existed
    lacks the column -- CREATE TABLE IF NOT EXISTS is a no-op against an
    already-created table, so opening it must not raise. A run with no
    surviving trace in observations or rate_changes (this legacy database
    has neither table populated at all) has no way to recover its actual
    product, so it keeps the home_loan default rather than the migration
    failing outright."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(str(db_path))
    connection.execute(
        "CREATE TABLE collection_runs ("
        "run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, banks TEXT NOT NULL, "
        "record_count INTEGER NOT NULL DEFAULT 0, inserted_count INTEGER NOT NULL DEFAULT 0, "
        "unchanged_count INTEGER NOT NULL DEFAULT 0, report_json TEXT)"
    )
    connection.execute(
        "INSERT INTO collection_runs (run_id, started_at, banks) VALUES (?, ?, ?)",
        ("legacy-run", DAY_ONE.isoformat(), "[]"),
    )
    connection.commit()
    connection.close()

    with ObservationStore(str(db_path)) as store:
        row = store._connection.execute(
            "SELECT product_type FROM collection_runs WHERE run_id = 'legacy-run'"
        ).fetchone()
        assert row["product_type"] == "home_loan"


def test_read_only_store_does_not_migrate_the_database_file_on_disk(tmp_path):
    """A dry run opening ``read_only=True`` against an older production
    database (missing a column a later migration adds) must not leave that
    migration's ``ALTER TABLE`` on disk -- CLI ``--dry-run`` promises nothing
    persisted, and that has to hold even when the on-disk schema is stale
    enough to need a migration just to be queryable. Confirmed by opening a
    legacy ``collector_health`` table (predating ``last_processed_run_id``),
    then reopening a raw connection afterwards: the column must still be
    absent from the file, even though the read-only store itself queried
    successfully as if it were present."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(str(db_path))
    connection.execute(
        "CREATE TABLE collector_health ("
        "bank TEXT NOT NULL, product_type TEXT NOT NULL, "
        "consecutive_failures INTEGER NOT NULL DEFAULT 0, "
        "last_success_at TEXT, last_failure_at TEXT, last_error TEXT, "
        "last_alert_sent_at TEXT, PRIMARY KEY (bank, product_type))"
    )
    connection.execute(
        "INSERT INTO collector_health (bank, product_type, consecutive_failures) "
        "VALUES ('ANZ', 'home_loan', 2)"
    )
    connection.commit()
    connection.close()

    with ObservationStore(str(db_path), read_only=True) as store:
        columns = {row["name"] for row in store._connection.execute("PRAGMA table_info(collector_health)")}
        assert "last_processed_run_id" in columns

    raw = sqlite3.connect(str(db_path))
    on_disk_columns = {row[1] for row in raw.execute("PRAGMA table_info(collector_health)")}
    raw.close()
    assert "last_processed_run_id" not in on_disk_columns


def test_read_only_store_snapshots_committed_wal_data(tmp_path, make_record):
    """A production database can run in WAL mode, where a committed write
    sits only in the '-wal' sidecar until checkpointed -- a plain file copy
    of just the main '.db' file would silently miss it, showing a dry run
    stale or empty data. The snapshot must use sqlite's own backup API (the
    way backup_database() already does), which reads through a live
    connection and always sees committed WAL pages too."""
    import sqlite3

    db_path = tmp_path / "wal.db"

    with ObservationStore(str(db_path)):
        pass
    raw = sqlite3.connect(str(db_path))
    raw.execute("PRAGMA journal_mode=WAL")
    raw.close()

    # Keep this writer connection open for the whole test: SQLite
    # auto-checkpoints a WAL database when its *last* connection closes,
    # which would flush the write below into the main file on its own and
    # mask the exact bug this test exists to catch.
    live_store = ObservationStore(str(db_path))
    live_store.record_observations([make_record()], observed_at=DAY_ONE)

    with ObservationStore(str(db_path), read_only=True) as snapshot:
        assert snapshot.get_current_observations() != []

    live_store.close()


def test_read_only_store_against_a_missing_database_does_not_create_a_file(tmp_path):
    """A dry run's first-ever invocation, before any real collection has
    run, points at a database path that doesn't exist yet. It must stay that
    way -- opening a read-only store should serve an empty in-memory
    database rather than materialising a fresh file on disk."""
    db_path = tmp_path / "does-not-exist-yet.db"

    with ObservationStore(str(db_path), read_only=True) as store:
        assert store.get_current_observations() == []

    assert not db_path.exists()


def test_read_only_store_source_open_rejects_a_toctou_removed_database(tmp_path, monkeypatch):
    """The exists() check above the WAL-aware snapshot leaves a narrow
    time-of-check-to-time-of-use gap -- the configured database can vanish
    (e.g. a restore or rotation) between that check and the source
    connection. Simulated here by making exists() lie: the read-only URI
    connection must still fail loudly with sqlite3.OperationalError rather
    than an ordinary read-write sqlite3.connect() silently creating a
    fresh, empty database at the real configured path -- exactly the
    artifact a dry run promises never to leave behind."""
    import os
    import sqlite3
    from pathlib import Path as PathlibPath

    db_path = tmp_path / "does-not-exist.db"
    monkeypatch.setattr(PathlibPath, "exists", lambda self: True)

    with pytest.raises(sqlite3.OperationalError):
        ObservationStore(str(db_path), read_only=True)

    assert not os.path.exists(str(db_path)), "must not have created an empty database"


def test_a_pre_column_database_infers_product_from_its_existing_history(store, make_record):
    """The --product CLI flag has collected term deposits since before
    collection_runs.product_type existed, so an upgrading database can
    already hold genuine deposit runs -- the migration must not mislabel
    them home_loan just because the column itself is new. Simulated here by
    collecting normally (which populates observations/rate_changes exactly
    as a pre-migration database would have) and then dropping the column to
    force the migration to re-infer it from that history."""
    deposit_record = make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")
    deposit_write = store.record_observations([deposit_record], observed_at=DAY_ONE)
    store.record_changes(
        [
            RateChange(
                observation_key=deposit_record.observation_key(),
                bank=deposit_record.bank,
                product_type=ProductType.TERM_DEPOSIT,
                product_name=deposit_record.product_name,
                rate_category=deposit_record.rate_category,
                term_months=deposit_record.term_months_normalised,
                change_type=ChangeType.NEW_PRODUCT,
                detected_at=DAY_ONE,
                current_rate_pct=deposit_record.advertised_rate_pct,
                run_id=deposit_write.run_id,
            )
        ]
    )
    store.record_observations([make_record(product_type=ProductType.HOME_LOAN)], observed_at=DAY_TWO)

    # Simulate a database that predates the column: drop it, then reopen
    # (re-running _create_schema()'s migration) as a fresh ObservationStore
    # would when it first encounters that older file.
    store._connection.execute("ALTER TABLE collection_runs DROP COLUMN product_type")
    store._create_schema()

    deposit_run = store.latest_run_id(product_type=ProductType.TERM_DEPOSIT)
    loan_run = store.latest_run_id(product_type=ProductType.HOME_LOAN)
    assert deposit_run is not None
    assert loan_run is not None
    assert deposit_run != loan_run


def test_a_pre_column_migration_recovers_a_reconfirmed_bootstrap_run(store, make_record):
    """A legacy deposit bootstrap run (the first-ever collection for that
    product) records zero rate_changes -- detect_changes()'s own bootstrap
    suppression means the rate_changes-based inference pass alone can't
    reach it. If its rows are later reconfirmed unchanged, last_seen_run_id
    also advances past it, so the observations-based pass can't reach it
    either. Its first_observed_at, frozen at insertion, is the only
    surviving trace -- the migration must still use it rather than falling
    through to the home_loan default."""
    bootstrap_record = make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")
    bootstrap_write = store.record_observations([bootstrap_record], observed_at=DAY_ONE)
    # No record_changes() call: a genuine bootstrap run detects nothing (see
    # detect_changes()'s bootstrap suppression), so rate_changes stays empty
    # for this run_id.

    # A later, unchanged reconfirmation of the same row advances
    # last_seen_run_id forward, away from the bootstrap run.
    store.record_observations([bootstrap_record], observed_at=DAY_TWO)

    # Simulate a database that predates the column: drop it, then reopen.
    store._connection.execute("ALTER TABLE collection_runs DROP COLUMN product_type")
    store._create_schema()

    row = store._connection.execute(
        "SELECT product_type FROM collection_runs WHERE run_id = ?",
        (bootstrap_write.run_id,),
    ).fetchone()
    assert row["product_type"] == "term_deposit"


def test_a_pre_column_migration_does_not_overwrite_a_correctly_resolved_product(
    store, make_record
):
    """Pass three of _migrate_collection_runs_product_type (started_at-value
    matching, unordered) must not clobber a run pass one or two already
    resolved with real evidence -- two different-product runs sharing a
    legacy started_at would otherwise both end up mislabelled with whichever
    product pass three's unordered pick happens to prefer."""
    loan_record = make_record(product_type=ProductType.HOME_LOAN)
    deposit_record = make_record(
        product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit"
    )
    loan_write = store.record_observations([loan_record], observed_at=DAY_ONE)
    deposit_write = store.record_observations([deposit_record], observed_at=DAY_ONE)

    # Simulate a database that predates the column.
    store._connection.execute("ALTER TABLE collection_runs DROP COLUMN product_type")
    store._create_schema()

    loan_row = store._connection.execute(
        "SELECT product_type FROM collection_runs WHERE run_id = ?", (loan_write.run_id,)
    ).fetchone()
    deposit_row = store._connection.execute(
        "SELECT product_type FROM collection_runs WHERE run_id = ?", (deposit_write.run_id,)
    ).fetchone()
    assert loan_row["product_type"] == "home_loan"
    assert deposit_row["product_type"] == "term_deposit"


def test_a_pre_column_migration_prefers_rate_changes_evidence_over_timestamp_ordering(
    store, make_record
):
    """Two legacy runs for the same product sharing a timestamp are
    inherently ambiguous by (timestamp, rowid) alone -- but a NEW_PRODUCT
    event recorded for a specific observation_key at a specific
    first_observed_at names its introducing run exactly. Preferring that
    exact match avoids picking the wrong (earliest-rowid) run when a later
    same-timestamp run is the one that actually introduced the row."""
    anz_record = make_record(bank="ANZ", rate_pct=4.79)
    store.record_observations([anz_record], run_id="run-first", observed_at=DAY_ONE)

    bnz_record = make_record(bank="BNZ", rate_pct=4.85)
    second_run = store.record_observations([bnz_record], run_id="run-second", observed_at=DAY_ONE)
    changes = detect_changes(
        previous=[],
        current=[bnz_record],
        detected_at=DAY_ONE,
        banks_collected=["BNZ"],
        run_id=second_run.run_id,
    )
    store.record_changes(changes)

    # Simulate a database that predates the column.
    store._connection.execute("ALTER TABLE observations DROP COLUMN first_seen_run_id")
    store._create_schema()

    row = store._connection.execute(
        "SELECT first_seen_run_id FROM observations WHERE observation_key = ?",
        (bnz_record.observation_key(),),
    ).fetchone()
    assert row["first_seen_run_id"] == second_run.run_id


def test_a_pre_column_database_migrates_first_seen_run_id(store, make_record):
    """A database created before observations.first_seen_run_id existed
    must gain it via migration (backfilled from the matching
    collection_runs row, scoped by product_type to avoid the same
    cross-contamination a live, timestamp-only correlation would risk --
    see _migrate_observations_first_seen_run_id()'s docstring), not raise
    when opened, and not merely default to NULL forever."""
    record = make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")
    write_result = store.record_observations([record], observed_at=DAY_ONE)

    # Simulate a database that predates the column: drop it, then reopen.
    store._connection.execute("ALTER TABLE observations DROP COLUMN first_seen_run_id")
    store._create_schema()

    row = store._connection.execute(
        "SELECT first_seen_run_id FROM observations WHERE observation_key = ?",
        (record.observation_key(),),
    ).fetchone()
    assert row["first_seen_run_id"] == write_result.run_id


def test_a_mixed_legacy_bootstrap_run_backfills_first_seen_run_id_for_both_products(
    store, make_record
):
    """A pre-column bootstrap run that collected both products at once has
    exactly one collection_runs row, so _migrate_collection_runs_product_type
    can only backfill a single product_type label onto that row (whichever
    its own priority passes recovered evidence for). Without a fallback,
    the *other* product's observations would find no collection_runs row
    matching their own product_type at that timestamp and be left with
    first_seen_run_id permanently NULL -- invisible to
    get_previous_run_started_at() for that product even though the run
    genuinely covered it. See _migrate_observations_first_seen_run_id()'s
    docstring for the unfiltered fallback pass that recovers this."""
    loan_record = make_record(product_type=ProductType.HOME_LOAN)
    deposit_record = make_record(
        product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit"
    )
    # Same run_id for both calls simulates one physical legacy run that
    # collected both products at once, before product_type was tracked
    # per collection.
    store.record_observations([loan_record], run_id="legacy-run", observed_at=DAY_ONE)
    _reuse_run_id_for_another_product(
        store, "legacy-run", [deposit_record], DAY_ONE, ProductType.TERM_DEPOSIT
    )

    # Simulate a database that predates both columns.
    store._connection.execute("ALTER TABLE observations DROP COLUMN first_seen_run_id")
    store._connection.execute("ALTER TABLE collection_runs DROP COLUMN product_type")
    store._create_schema()

    rows = store._connection.execute(
        "SELECT observation_key, first_seen_run_id FROM observations ORDER BY observation_key"
    ).fetchall()
    assert len(rows) == 2
    assert all(row["first_seen_run_id"] == "legacy-run" for row in rows)


def test_a_pre_column_migration_does_not_attribute_a_same_timestamp_return_to_the_bootstrap_row(
    store, make_record
):
    """A bootstrap row and a later return row can share first_observed_at
    when the return happens to be backfilled with the bootstrap run's own
    timestamp (a legacy-data scenario). Both rows then tie on
    (observation_key, first_observed_at), but only the return produced a
    NEW_PRODUCT event -- the migration's exact-event pass must not let its
    correlated subquery match that one event to both tied rows and hand the
    return's run_id to the untouched bootstrap row too."""
    bnz_record = make_record(bank="BNZ", rate_pct=4.85)
    store.record_observations([bnz_record], run_id="run-bootstrap", observed_at=DAY_ONE)
    # No record_changes() call: a genuine bootstrap run detects nothing.

    withdraw_run = store.record_observations([], run_id="run-withdrawn", observed_at=DAY_TWO)
    store.record_changes(
        detect_changes(
            previous=store.get_current_observations(),
            current=[],
            detected_at=DAY_TWO,
            banks_collected=["BNZ"],
            run_id=withdraw_run.run_id,
        )
    )

    # The return is backfilled with the bootstrap run's own timestamp,
    # tying first_observed_at across the two observations rows.
    return_run = store.record_observations(
        [bnz_record], run_id="run-returned", observed_at=DAY_TWO
    )
    # Construct legacy backdated data directly; new writes now reject it.
    store._connection.execute(
        "UPDATE observations SET first_observed_at = ? WHERE first_seen_run_id = ?",
        (DAY_ONE.isoformat(), "run-returned"),
    )
    store._connection.execute(
        "UPDATE collection_runs SET started_at = ? WHERE run_id = ?",
        (DAY_ONE.isoformat(), "run-returned"),
    )
    store.record_changes(
        detect_changes(
            previous=[],
            current=[bnz_record],
            detected_at=DAY_ONE,
            banks_collected=["BNZ"],
            run_id=return_run.run_id,
        )
    )

    # Simulate a database that predates the column.
    store._connection.execute("ALTER TABLE observations DROP COLUMN first_seen_run_id")
    store._create_schema()

    rows = store._connection.execute(
        "SELECT id, first_seen_run_id FROM observations WHERE observation_key = ? ORDER BY id",
        (bnz_record.observation_key(),),
    ).fetchall()
    assert len(rows) == 2, "the withdrawal and the return must be distinct states"
    assert rows[0]["first_seen_run_id"] == "run-bootstrap"
    assert rows[1]["first_seen_run_id"] == "run-returned"


def test_simultaneous_single_product_runs_are_not_cross_contaminated(store, make_record):
    """A caller collecting both products from one shared capture moment
    (e.g. the same captured_at_utc passed to two separate, single-product
    record_observations() calls) produces two collection_runs rows with the
    same started_at but different run_ids. Correlating first-seen evidence
    by matching started_at values (rather than by the run_id
    first_seen_run_id actually references) would make each run appear to
    contain the other's rows too, and latest_run_id()'s own
    ORDER BY ... rowid DESC tie-break could then return the wrong run_id
    for either product -- leaving get_latest_rates() for one product
    filtering on a last_seen_run_id nothing points to."""
    home_loan_record = make_record(product_type=ProductType.HOME_LOAN)
    deposit_record = make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")
    home_loan_write = store.record_observations([home_loan_record], observed_at=DAY_ONE)
    deposit_write = store.record_observations([deposit_record], observed_at=DAY_ONE)
    assert home_loan_write.run_id != deposit_write.run_id
    assert store.get_run_started_at(home_loan_write.run_id) == store.get_run_started_at(
        deposit_write.run_id
    )

    assert store.latest_run_id(product_type=ProductType.HOME_LOAN) == home_loan_write.run_id
    assert store.latest_run_id(product_type=ProductType.TERM_DEPOSIT) == deposit_write.run_id
    assert [r.bank for r in store.get_latest_rates(product_type=ProductType.HOME_LOAN)] == [
        home_loan_record.bank
    ]
    assert [
        r.bank for r in store.get_latest_rates(product_type=ProductType.TERM_DEPOSIT)
    ] == [deposit_record.bank]


def test_migrating_collection_run_products_preserves_original_run_order(store, make_record):
    """The migration's own backfill passes can distort relative ordering:
    pass 1 processes collection_runs in one sweep (whichever product it
    currently labels), and passes 2-4 append whatever the *other* product
    of a mixed run needs in later sweeps -- so a mixed run's second product
    can land in collection_run_products with a rowid that comes *after* a
    genuinely later, single-product run's own row, even though the mixed
    run's collection_runs.rowid (its one true insertion position) precedes
    it. latest_run_id()/_previous_run_row() must order by
    collection_runs.rowid, not collection_run_products' own, or reopening
    such a database inverts the two runs' order."""
    shared_run_id = "legacy-mixed-run"
    store.record_observations(
        [make_record(bank="ANZ", product_type=ProductType.HOME_LOAN, rate_pct=4.79)],
        run_id=shared_run_id,
        observed_at=DAY_ONE,
    )
    # Overwrites collection_runs.product_type to term_deposit -- ANZ's
    # home-loan row is now only findable via last_seen_run_id/first_seen_run_id.
    _reuse_run_id_for_another_product(
        store,
        shared_run_id,
        [make_record(bank="ANZ", product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")],
        DAY_ONE,
        ProductType.TERM_DEPOSIT,
    )

    # A genuinely later home-loan run, sharing the exact same timestamp, for
    # an unrelated bank -- so it never touches ANZ's own last_seen_run_id.
    store.record_observations(
        [make_record(bank="BNZ", product_type=ProductType.HOME_LOAN, rate_pct=4.99)],
        run_id="later-home-loan-run",
        observed_at=DAY_ONE,
    )

    # Simulate a database whose collection_run_products needs (re)backfilling
    # from scratch -- e.g. one created before the table existed at all.
    store._connection.execute("DELETE FROM collection_run_products")
    store._create_schema()

    assert store.latest_run_id(product_type=ProductType.HOME_LOAN) == "later-home-loan-run"


def test_live_participation_order_is_preserved_for_a_reused_run_id(store, make_record):
    """From this store's transitional era -- collection_run_products
    existed, but record_observations() had not yet refused reusing a run_id
    for a second product (see
    test_reusing_a_run_id_for_a_different_product_is_rejected): "shared" is
    first used for home loans, "middle" records a deposit at the same
    timestamp, then "shared" is reused for an unchanged reconfirmation of
    that same deposit. collection_run_products' own rowid correctly orders
    shared's deposit participation *after* middle's -- it was genuinely
    recorded later -- even though collection_runs.rowid alone would rank
    shared first (it was created earlier, for home loans). latest_run_id()
    must prefer collection_run_products' own rowid whenever it has a row,
    not fall back to collection_runs.rowid unconditionally, or it would
    rank "middle" above "shared" and find no rows for
    get_latest_rates(TERM_DEPOSIT) -- the deposit's actual last_seen_run_id
    points at "shared", not "middle"."""
    shared_run_id = "shared"
    store.record_observations(
        [make_record(bank="ANZ", product_type=ProductType.HOME_LOAN, rate_pct=4.79)],
        run_id=shared_run_id,
        observed_at=DAY_ONE,
    )
    deposit_record = make_record(
        bank="ANZ", product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit"
    )
    store.record_observations([deposit_record], run_id="middle", observed_at=DAY_ONE)
    # Simulates "shared" being reused for an unchanged deposit
    # reconfirmation, from before record_observations() refused that.
    _reuse_run_id_for_another_product(
        store, shared_run_id, [deposit_record], DAY_ONE, ProductType.TERM_DEPOSIT
    )

    assert store.latest_run_id(product_type=ProductType.TERM_DEPOSIT) == shared_run_id
    assert [
        record.bank for record in store.get_latest_rates(product_type=ProductType.TERM_DEPOSIT)
    ] == ["ANZ"]


def test_a_legacy_run_that_mixed_both_products_stays_findable_under_either(store, make_record):
    """Before record_observations() rejected a mixed batch, a database could
    hold a legacy run whose single collection_runs row ends up labelled with
    just one product even though its observations span both -- one run_id
    can only store one product_type value. The stored column alone would
    make that run invisible to latest_run_id() for whichever product lost
    the tie-break; observations/rate_changes must still recover it."""
    shared_run_id = "legacy-mixed-run"
    store.record_observations(
        [make_record(product_type=ProductType.HOME_LOAN)],
        run_id=shared_run_id,
        observed_at=DAY_ONE,
    )
    # Simulates the other product's call under this same legacy run_id --
    # exactly the ambiguity a genuinely mixed legacy run leaves behind for
    # the home-loan side, since collection_runs' stored product_type ends
    # up naming whichever product's call happened last.
    _reuse_run_id_for_another_product(
        store,
        shared_run_id,
        [make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")],
        DAY_ONE,
        ProductType.TERM_DEPOSIT,
    )

    assert store.latest_run_id(product_type=ProductType.HOME_LOAN) == shared_run_id
    assert store.latest_run_id(product_type=ProductType.TERM_DEPOSIT) == shared_run_id


def test_a_mixed_bootstrap_run_is_findable_under_either_product_after_reconfirmation(
    store, make_record
):
    """The mixed-run and bootstrap-run recovery mechanisms combined: a
    legacy run that mixed both products *and* was a bootstrap (zero
    rate_changes for either product, since a first-ever collection detects
    nothing) leaves neither product's observations pointing back to it via
    last_seen_run_id once both are later reconfirmed unchanged. Only
    first_observed_at still does -- the scoped queries must check it too,
    not just rely on it having been enough to label the stored column
    during migration."""
    shared_run_id = "legacy-mixed-bootstrap-run"
    home_loan_record = make_record(product_type=ProductType.HOME_LOAN)
    deposit_record = make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")
    store.record_observations([home_loan_record], run_id=shared_run_id, observed_at=DAY_ONE)
    # Overwrites collection_runs' stored product_type to term_deposit --
    # the home-loan side now has nothing but first_observed_at pointing
    # back to this run.
    _reuse_run_id_for_another_product(
        store, shared_run_id, [deposit_record], DAY_ONE, ProductType.TERM_DEPOSIT
    )
    # No record_changes() call for shared_run_id at all: a genuine bootstrap
    # detects nothing for either product.

    # Separate, single-product reconfirmations (mixing them would itself hit
    # the same guard record_observations() now enforces) advance
    # last_seen_run_id for both products away from the bootstrap run.
    store.record_observations([home_loan_record], observed_at=DAY_TWO)
    store.record_observations([deposit_record], observed_at=DAY_TWO)

    assert store.get_previous_run_started_at(product_type=ProductType.HOME_LOAN) == DAY_ONE
    assert store.get_previous_run_started_at(product_type=ProductType.TERM_DEPOSIT) == DAY_ONE


def test_get_previous_run_started_at_finds_a_legacy_run_that_mixed_both_products(
    store, make_record
):
    """Same ambiguity as latest_run_id(), for the "previous run" query: a
    legacy run that mixed both products under one run_id must still be
    resolvable as the predecessor for either product, not just whichever one
    the stored column happened to end up labelled with. Deposit is written
    first here so the stored value ends up "home_loan" (the second/last
    write wins via INSERT OR REPLACE) -- the opposite of what this test asks
    for, so it only passes if the EXISTS fallback is actually doing the work."""
    shared_run_id = "legacy-mixed-run"
    store.record_observations(
        [make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")],
        run_id=shared_run_id,
        observed_at=DAY_ONE,
    )
    _reuse_run_id_for_another_product(
        store,
        shared_run_id,
        [make_record(product_type=ProductType.HOME_LOAN)],
        DAY_ONE,
        ProductType.HOME_LOAN,
    )
    store.record_observations(
        [
            make_record(
                product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit", rate_pct=4.50
            )
        ],
        observed_at=DAY_TWO,
    )

    assert store.get_previous_run_started_at(product_type=ProductType.TERM_DEPOSIT) == DAY_ONE


def test_get_previous_run_started_at_finds_a_same_timestamp_predecessor(store, make_record):
    """Two runs for the same product can share observed_at -- record_observations()'s
    public API allows an explicit timestamp, and latest_run_id() already
    breaks such ties on insertion order (rowid). A strict "started_at <
    reference" comparison would instead skip a same-timestamp predecessor
    entirely and return an even older run, producing an incorrect
    "previous market snapshot" comparison in get_previous_market_snapshot()."""
    store.record_observations([make_record(rate_pct=4.50)], observed_at=DAY_ONE)
    store.record_observations([make_record(rate_pct=4.60)], observed_at=DAY_TWO)
    later = store.record_observations([make_record(rate_pct=4.70)], observed_at=DAY_TWO)

    assert store.get_previous_run_started_at(
        before_run_id=later.run_id, product_type=ProductType.HOME_LOAN
    ) == DAY_TWO


def test_get_previous_run_started_at_unscoped_finds_a_same_timestamp_predecessor(
    store, make_record
):
    """Same fix, unscoped query path (product_type=None)."""
    store.record_observations([make_record(rate_pct=4.50)], observed_at=DAY_ONE)
    store.record_observations([make_record(rate_pct=4.60)], observed_at=DAY_TWO)
    later = store.record_observations([make_record(rate_pct=4.70)], observed_at=DAY_TWO)

    assert store.get_previous_run_started_at(before_run_id=later.run_id) == DAY_TWO


def test_previous_run_rates_correctly_resolve_a_same_timestamp_predecessor(store, make_record):
    """Two runs for the same product sharing observed_at, with the rate
    changing between them, must not have get_previous_run_rates() report the
    LATER run's rate as also being the PREVIOUS run's rate. A timestamp-only
    reconstruction can't tell the two same-timestamp runs apart -- without
    anchoring to the resolved predecessor's own run identity, the later
    run's row also satisfies "first_observed_at <= as_of" (they're equal)
    and wins the per-key selection."""
    store.record_observations([make_record(rate_pct=4.60)], observed_at=DAY_TWO)
    store.record_observations([make_record(rate_pct=4.70)], observed_at=DAY_TWO)

    previous = store.get_previous_run_rates()

    assert [record.advertised_rate_pct for record in previous] == [4.60]


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
    write = store.record_observations(current, observed_at=DAY_TWO)
    changes = detect_changes(
        previous=baseline,
        current=current,
        detected_at=DAY_TWO,
        banks_collected=["ANZ", "BNZ"],
        run_id=write.run_id,
    )
    store.record_changes(changes)


def test_current_observations_exclude_a_confirmed_withdrawal(store, make_record):
    """Unlike a transient collection failure, a recorded withdrawal must not linger."""
    _withdraw_bnz_on_day_two(store, make_record)

    assert {record.bank for record in store.get_current_observations()} == {"ANZ"}


def test_current_observations_exclude_a_same_timestamp_withdrawal(store, make_record):
    """A product introduced in the earlier of two same-timestamp runs and
    withdrawn in the later one must not linger in the current view --
    rc.detected_at and the row's own last_seen_at tie at the identical
    timestamp, so the withdrawal predicate needs run position, not a plain
    detected_at > last_seen_at comparison, to recognise the withdrawal as
    coming after the row was last confirmed."""
    store.record_observations(
        [make_record(bank="BNZ", rate_pct=4.85)], run_id="run-earlier", observed_at=DAY_TWO
    )
    later = store.record_observations([], run_id="run-later", observed_at=DAY_TWO)
    changes = detect_changes(
        previous=store.get_current_observations(),
        current=[],
        detected_at=DAY_TWO,
        banks_collected=["BNZ"],
        run_id=later.run_id,
    )
    store.record_changes(changes)

    assert store.get_current_observations() == []


def test_repeated_same_timestamp_withdrawals_are_both_recorded(store, make_record):
    """Four runs sharing one started_at -- observed, withdrawn, returned,
    withdrawn again -- must record a REMOVED_PRODUCT event for *each*
    withdrawal, not just the first. rate_changes' uniqueness scoped only to
    (observation_key, change_type, detected_at) would otherwise collide the
    second withdrawal's event with the first's (same key, same change_type,
    same shared timestamp) and silently drop it via INSERT OR IGNORE,
    leaving the returned-then-withdrawn row in get_current_observations()
    with no REMOVED_PRODUCT recorded after its last (return) touch."""
    bnz_record = make_record(bank="BNZ", rate_pct=4.85)

    store.record_observations([bnz_record], run_id="run-1-observed", observed_at=DAY_TWO)
    # No record_changes() call: a genuine bootstrap run detects nothing.

    withdrawn_once = store.record_observations([], run_id="run-2-withdrawn", observed_at=DAY_TWO)
    store.record_changes(
        detect_changes(
            previous=store.get_current_observations(),
            current=[],
            detected_at=DAY_TWO,
            banks_collected=["BNZ"],
            run_id=withdrawn_once.run_id,
        )
    )

    returned = store.record_observations([bnz_record], run_id="run-3-returned", observed_at=DAY_TWO)
    store.record_changes(
        detect_changes(
            previous=store.get_current_observations(),
            current=[bnz_record],
            detected_at=DAY_TWO,
            banks_collected=["BNZ"],
            run_id=returned.run_id,
        )
    )
    assert {record.bank for record in store.get_current_observations()} == {"BNZ"}

    withdrawn_again = store.record_observations(
        [], run_id="run-4-withdrawn-again", observed_at=DAY_TWO
    )
    store.record_changes(
        detect_changes(
            previous=store.get_current_observations(),
            current=[],
            detected_at=DAY_TWO,
            banks_collected=["BNZ"],
            run_id=withdrawn_again.run_id,
        )
    )

    assert store.get_current_observations() == []


def test_migrating_rate_changes_widens_uniqueness_without_losing_existing_rows(store, make_record):
    """A database created before run_id joined rate_changes' UNIQUE
    constraint must be upgraded in place, keeping its existing event, and
    afterwards accept a second event that only the old three-column
    constraint would have collided with (same observation_key/change_type/
    detected_at, a different run_id) -- exactly the same-timestamp
    repeated-withdrawal scenario this widening exists to fix."""
    bnz_record = make_record(bank="BNZ", rate_pct=4.85)
    write = store.record_observations([bnz_record], observed_at=DAY_ONE)
    store.record_changes(
        [
            RateChange(
                observation_key=bnz_record.observation_key(),
                bank=bnz_record.bank,
                product_type=ProductType(bnz_record.product_type),
                product_name=bnz_record.product_name,
                rate_category=bnz_record.rate_category,
                term_months=bnz_record.term_months_normalised,
                change_type=ChangeType.NEW_PRODUCT,
                detected_at=DAY_ONE,
                current_rate_pct=bnz_record.advertised_rate_pct,
                run_id=write.run_id,
            )
        ]
    )

    # Simulate a database that predates run_id joining the UNIQUE constraint:
    # rebuild rate_changes under the old three-column definition, preserving
    # its one existing row.
    store._connection.executescript(
        "ALTER TABLE rate_changes RENAME TO rate_changes_new;"
        "DROP INDEX idx_changes_detected_at;"
        "CREATE TABLE rate_changes ("
        "    id INTEGER PRIMARY KEY AUTOINCREMENT, observation_key TEXT NOT NULL, "
        "    bank TEXT NOT NULL, product_type TEXT NOT NULL, product_name TEXT NOT NULL, "
        "    rate_category TEXT NOT NULL, term_months INTEGER, change_type TEXT NOT NULL, "
        "    previous_rate_pct REAL, current_rate_pct REAL, change_bps REAL, "
        "    previous_observed_at TEXT, detected_at TEXT NOT NULL, run_id TEXT NOT NULL, "
        "    suspicious INTEGER NOT NULL DEFAULT 0, detail TEXT, source_url TEXT, "
        "    UNIQUE (observation_key, change_type, detected_at)"
        ");"
        "CREATE INDEX idx_changes_detected_at ON rate_changes (detected_at);"
        "INSERT INTO rate_changes SELECT * FROM rate_changes_new;"
        "DROP TABLE rate_changes_new;"
    )

    store._create_schema()

    rows = store._connection.execute("SELECT run_id FROM rate_changes").fetchall()
    assert [row["run_id"] for row in rows] == [write.run_id]

    # ALTER TABLE ... RENAME carries an index across under its original name,
    # so a migration that recreates the table without first dropping that
    # index would find CREATE INDEX IF NOT EXISTS a no-op (name already
    # taken by the renamed-away old table) and then lose it entirely once
    # the old table is dropped -- leaving detected_at with no index at all.
    detected_at_indexes = [
        index["name"]
        for index in store._connection.execute("PRAGMA index_list('rate_changes')")
        if {
            info["name"] for info in store._connection.execute(f"PRAGMA index_info({index['name']!r})")
        }
        == {"detected_at"}
    ]
    assert detected_at_indexes, "rate_changes must still have an index on detected_at"

    # Re-running the migration on an already-upgraded database must be a
    # no-op, not a repeated rebuild.
    store._create_schema()
    rows = store._connection.execute("SELECT run_id FROM rate_changes").fetchall()
    assert [row["run_id"] for row in rows] == [write.run_id]

    # The exact scenario the widened constraint exists for: a second event
    # for the same key/change_type/detected_at, from a different run, must
    # no longer collide with the first.
    store.record_changes(
        [
            RateChange(
                observation_key=bnz_record.observation_key(),
                bank=bnz_record.bank,
                product_type=ProductType(bnz_record.product_type),
                product_name=bnz_record.product_name,
                rate_category=bnz_record.rate_category,
                term_months=bnz_record.term_months_normalised,
                change_type=ChangeType.NEW_PRODUCT,
                detected_at=DAY_ONE,
                current_rate_pct=bnz_record.advertised_rate_pct,
                run_id="a-different-run",
            )
        ]
    )

    run_ids = {row["run_id"] for row in store._connection.execute("SELECT run_id FROM rate_changes")}
    assert run_ids == {write.run_id, "a-different-run"}


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
    write_at_run_two = store.record_observations(transitioned, observed_at=DAY_TWO)
    changes_at_run_two = detect_changes(
        previous=baseline_at_run_two,
        current=transitioned,
        detected_at=DAY_TWO,
        banks_collected=["ANZ"],
        run_id=write_at_run_two.run_id,
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


def test_observations_as_of_do_not_apply_a_same_timestamp_later_eligibility_edit(
    store, make_record
):
    """An eligibility transition recorded by a LATER same-timestamp run must
    not retroactively supersede the prior state for a reconstruction anchored
    at an EARLIER same-timestamp run -- the same run-order requirement
    already applied to the withdrawal predicate, here applied to
    _SUPERSEDED_BY_LATER_ELIGIBILITY_KEY's {extra} bound."""
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79)], observed_at=DAY_ONE
    )
    # A bank unreachable in the earlier of two same-timestamp runs...
    earlier = store.record_observations([], run_id="run-earlier", observed_at=DAY_TWO)
    # ...returns in the later one with a changed eligibility key.
    store.record_observations(
        [make_record(max_lvr=0.75, min_equity=0.25, rate_pct=4.99)],
        run_id="run-later",
        observed_at=DAY_TWO,
    )

    as_of_earlier = store.get_observations_as_of(as_of=DAY_TWO, before_run_id=earlier.run_id)

    assert [r.max_lvr for r in as_of_earlier] == [0.80]


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


def test_a_reverted_eligibility_condition_starts_a_fresh_state_across_same_timestamp_runs(
    store, make_record
):
    """Same fix as the run-order withdrawal/return comparison, applied to
    the eligibility-transition path: an eligibility condition flipping
    A -> B -> A across three runs sharing one observed_at must still start
    a fresh state on the A return, not silently merge into the stale
    pre-transition row. Under a plain first_observed_at > last_seen_at
    timestamp check, B's first_observed_at ties with A's last_seen_at (both
    DAY_TWO), so the transition would go undetected, the return would be
    treated as an unchanged reconfirmation of the original row, and a later
    run-anchored reconstruction of the middle run would no longer see B as
    superseding A."""
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=5.19)],
        run_id="run-a",
        observed_at=DAY_TWO,
    )
    store.record_observations(
        [make_record(max_lvr=0.75, min_equity=0.25, rate_pct=4.99)],  # new eligibility key
        run_id="run-b",
        observed_at=DAY_TWO,
    )
    # Reverts to the exact same values as state A, at the same observed_at.
    result = store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=5.19)],
        run_id="run-a-return",
        observed_at=DAY_TWO,
    )

    assert result.inserted == 1, "must start a fresh state, not silently merge into the stale row"

    key = make_record(max_lvr=0.80, min_equity=0.20, rate_pct=5.19).observation_key()
    row = store._connection.execute(
        "SELECT COUNT(*) AS n FROM observations WHERE observation_key = ?", (key,)
    ).fetchone()
    assert row["n"] == 2, "the original and reverted states must be distinct rows"


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


def test_observations_as_of_do_not_apply_a_same_timestamp_later_withdrawal(store, make_record):
    """A REMOVED_PRODUCT event recorded by a LATER same-timestamp run must not
    retroactively apply to a reconstruction anchored at an EARLIER
    same-timestamp run. rate_changes.detected_at ties the same way
    observations.first_observed_at does, so the withdrawal predicate needs
    the identical run-order anchoring, via rate_changes.run_id -- not just
    the observation-row cutoff."""
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79), make_record(bank="BNZ", rate_pct=4.85)],
        observed_at=DAY_ONE,
    )
    baseline = store.get_current_observations()

    earlier = store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79)], run_id="run-earlier", observed_at=DAY_TWO
    )
    later = store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79)], run_id="run-later", observed_at=DAY_TWO
    )
    changes = detect_changes(
        previous=baseline,
        current=[make_record(bank="ANZ", rate_pct=4.79, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
        banks_collected=["ANZ", "BNZ"],
        run_id=later.run_id,
    )
    store.record_changes(changes)

    as_of_earlier = store.get_observations_as_of(as_of=DAY_TWO, before_run_id=earlier.run_id)
    as_of_later = store.get_observations_as_of(as_of=DAY_TWO, before_run_id=later.run_id)

    assert {record.bank for record in as_of_earlier} == {"ANZ", "BNZ"}
    assert {record.bank for record in as_of_later} == {"ANZ"}


def test_observations_as_of_apply_a_same_run_withdrawal_of_a_just_introduced_state(
    store, make_record
):
    """A product introduced in the earlier of two same-timestamp runs and
    withdrawn in the later one must be excluded once reconstruction reaches
    that later run. rc.detected_at and the state's own first_observed_at tie
    at the identical timestamp here, so the withdrawal predicate needs run
    position -- not a plain detected_at > first_observed_at comparison -- to
    recognise the withdrawal as coming after the introduction."""
    store.record_observations(
        [make_record(bank="BNZ", rate_pct=4.85)], run_id="run-earlier", observed_at=DAY_TWO
    )
    later = store.record_observations([], run_id="run-later", observed_at=DAY_TWO)
    changes = detect_changes(
        previous=store.get_current_observations(),
        current=[],
        detected_at=DAY_TWO,
        banks_collected=["BNZ"],
        run_id=later.run_id,
    )
    store.record_changes(changes)

    as_of_later = store.get_observations_as_of(as_of=DAY_TWO, before_run_id=later.run_id)

    assert as_of_later == []


def test_observations_as_of_apply_a_same_timestamp_later_eligibility_transition(
    store, make_record
):
    """An eligibility transition recorded by a LATER same-timestamp run must
    still retire the OLD state from a reconstruction anchored at that later
    run, even when the old state's own last confirmation happened at the
    identical timestamp -- o3.first_observed_at and o.last_seen_at tie here,
    so the supersession predicate needs run position, not a plain >
    timestamp comparison, to recognise the newer key as coming after."""
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79)], observed_at=DAY_ONE
    )
    store.record_observations(
        [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=4.79)],  # reconfirmed, unchanged
        run_id="run-earlier",
        observed_at=DAY_TWO,
    )
    later = store.record_observations(
        [make_record(max_lvr=0.75, min_equity=0.25, rate_pct=4.99)],  # new eligibility key
        run_id="run-later",
        observed_at=DAY_TWO,
    )

    as_of_later = store.get_observations_as_of(as_of=DAY_TWO, before_run_id=later.run_id)

    assert [record.max_lvr for record in as_of_later] == [0.75]


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


def test_a_product_returning_after_a_same_timestamp_withdrawal_starts_a_fresh_state(
    store, make_record
):
    """Same fix as the run-order withdrawal/eligibility comparisons, applied
    at write time: a product observed, withdrawn, and returned across three
    runs sharing the same observed_at must still start a fresh state on
    return, not merge into the stale pre-withdrawal row -- which would
    corrupt that row's first_seen_run_id for every later run-anchored
    reconstruction (get_market_snapshot(as_of_run_id=<return run>) would
    keep seeing the withdrawal as predating the row's own introduction and
    omit the product even though it has reappeared)."""
    store.record_observations(
        [make_record(bank="BNZ", rate_pct=4.85)], run_id="run-observed", observed_at=DAY_TWO
    )
    withdraw_run = store.record_observations([], run_id="run-withdrawn", observed_at=DAY_TWO)
    changes = detect_changes(
        previous=store.get_current_observations(),
        current=[],
        detected_at=DAY_TWO,
        banks_collected=["BNZ"],
        run_id=withdraw_run.run_id,
    )
    store.record_changes(changes)

    return_run = store.record_observations(
        [make_record(bank="BNZ", rate_pct=4.85)], run_id="run-returned", observed_at=DAY_TWO
    )

    history = store.get_rate_history(bank="BNZ")
    assert len(history) == 2, "the withdrawal and the return must be distinct states"

    snapshot = store.get_observations_as_of(as_of=DAY_TWO, before_run_id=return_run.run_id)
    assert [record.bank for record in snapshot] == ["BNZ"]


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


def test_last_confirmed_observations_exclude_a_same_timestamp_withdrawal(store, make_record):
    """Same fix as get_current_observations(), for the confirmed-state view."""
    store.record_observations(
        [make_record(bank="BNZ", rate_pct=4.85)], run_id="run-earlier", observed_at=DAY_TWO
    )
    later = store.record_observations([], run_id="run-later", observed_at=DAY_TWO)
    changes = detect_changes(
        previous=store.get_current_observations(),
        current=[],
        detected_at=DAY_TWO,
        banks_collected=["BNZ"],
        run_id=later.run_id,
    )
    store.record_changes(changes)

    assert store.get_last_confirmed_observations() == []


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
        run_id="run-two",
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
        run_id="run-three",
    )
    store.record_changes(changes)

    assert store.get_changes_since(days=7, now=DAY_TWO) == []
    assert len(store.get_changes_since(days=7, now=DAY_THREE)) == 1


def test_recording_the_same_change_twice_is_idempotent(store, make_record):
    changes = detect_changes(
        [make_record(rate_pct=4.99)],
        [make_record(rate_pct=4.79, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
        run_id="run-two",
    )
    store.record_changes(changes)
    store.record_changes(changes)

    assert len(store.get_changes_since(days=7, now=DAY_TWO)) == 1


def test_record_changes_rejects_a_change_with_no_run_id(store, make_record):
    """RateChange.run_id is optional at construction (a caller may only want
    a change for display/alerting and never persist it), but a shared
    placeholder for every omitted run_id at persistence time would let two
    genuinely different events collide on it exactly as they used to collide
    on a shared detected_at alone -- so record_changes() must refuse rather
    than silently substitute one."""
    changes = detect_changes(
        [make_record(rate_pct=4.99)],
        [make_record(rate_pct=4.79, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )

    with pytest.raises(ValueError, match="run_id"):
        store.record_changes(changes)


def test_changes_filter_by_bank_and_term(store, make_record):
    changes = detect_changes(
        [make_record(bank="ANZ", rate_pct=4.99), make_record(bank="BNZ", rate_pct=4.95)],
        [
            make_record(bank="ANZ", rate_pct=4.79, observed_at=DAY_TWO),
            make_record(bank="BNZ", rate_pct=4.85, observed_at=DAY_TWO),
        ],
        detected_at=DAY_TWO,
        run_id="run-two",
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
            run_id="run-two",
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
        run_id="run-two",
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
        run_id="run-two",
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
        run_id="run-two",
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


def test_legacy_rate_named_migration_survives_two_events_that_share_a_migrated_key(
    tmp_path, make_record
):
    """Two rate-named rows of one product collapse onto a single identity.

    ``rate_changes`` is unique on (observation_key, change_type, detected_at,
    run_id), so a legacy store where the old parser emitted a NEW_PRODUCT for
    each rate-named variant in the same run has two event rows that the
    migration rewrites onto the same key. The rewrite must skip the collision
    rather than raise: the migration runs inside ``_create_schema`` on every
    open, so an unhandled IntegrityError would leave the database permanently
    unopenable -- taking the CLI, the dashboard and the scheduled collector
    with it.
    """
    path = tmp_path / "legacy.db"
    dearer = make_record(
        bank="Westpac", rate_pct=5.59, product_name="5.59% p.a.", term_label_raw="1 year"
    )
    cheaper = make_record(
        bank="Westpac", rate_pct=5.49, product_name="5.49% p.a.", term_label_raw="1 year"
    )
    changes = detect_changes(
        previous=[], current=[dearer, cheaper], detected_at=DAY_ONE, run_id="run-one"
    )
    with ObservationStore(path) as store:
        store.record_observations([dearer, cheaper], observed_at=DAY_ONE)
        store.record_changes(changes)

    with ObservationStore(path) as reopened:
        assert [record.product_name for record in reopened.get_current_observations()] == ["1 year"]
        assert reopened.get_current_observations()[0].advertised_rate_pct == 5.49
        # The surviving event carries the repaired identity and is quarantined.
        migrated = [
            event
            for event in reopened.get_changes_since(days=7, now=DAY_ONE)
            if event["product_name"] == "1 year"
        ]
        assert migrated and all(event["suspicious"] for event in migrated)


def test_legacy_rate_named_migration_quarantines_a_skipped_collision_row_too(
    tmp_path, make_record
):
    """The event the identity rewrite skips (see the test above) stays under
    its original rate-named key -- but it must not stay *unflagged* there:
    UPDATE OR IGNORE only touches the row it succeeds on, so a plain
    combined UPDATE ... SET suspicious = 1 ... would leave the skipped row's
    suspicious flag at its original 0. get_changes_since(include_suspicious=
    False) and market-activity calculations would then treat this known
    parser artifact as a genuine, unflagged event."""
    path = tmp_path / "legacy.db"
    dearer = make_record(
        bank="Westpac", rate_pct=5.59, product_name="5.59% p.a.", term_label_raw="1 year"
    )
    cheaper = make_record(
        bank="Westpac", rate_pct=5.49, product_name="5.49% p.a.", term_label_raw="1 year"
    )
    changes = detect_changes(
        previous=[], current=[dearer, cheaper], detected_at=DAY_ONE, run_id="run-one"
    )
    with ObservationStore(path) as store:
        store.record_observations([dearer, cheaper], observed_at=DAY_ONE)
        store.record_changes(changes)

    with ObservationStore(path) as reopened:
        every_event = reopened.get_changes_since(days=7, now=DAY_ONE, include_suspicious=True)
        unflagged_events = reopened.get_changes_since(
            days=7, now=DAY_ONE, include_suspicious=False
        )
        assert len(every_event) == 2
        assert unflagged_events == []


def test_legacy_rate_named_migration_quarantines_the_colliding_observations_too(
    tmp_path, make_record
):
    """Flagging the rate_changes events (see the tests above) is not enough:
    the observations table has no uniqueness constraint on observation_key,
    so both colliding rows survive under the same repaired identity with a
    clean validation_status by default. get_current_observations() then
    picks whichever one has the higher id as the current state -- an
    accident of parse order, not a verified rate -- and get_market_snapshot()
    would trust it. Both colliding observations must be quarantined as
    validation_status='warning' too, the same convention a suspicious live
    collection already uses to keep a bad reading out of market
    statistics."""
    path = tmp_path / "legacy.db"
    dearer = make_record(
        bank="Westpac", rate_pct=5.59, product_name="5.59% p.a.", term_label_raw="1 year"
    )
    cheaper = make_record(
        bank="Westpac", rate_pct=5.49, product_name="5.49% p.a.", term_label_raw="1 year"
    )
    changes = detect_changes(
        previous=[], current=[dearer, cheaper], detected_at=DAY_ONE, run_id="run-one"
    )
    with ObservationStore(path) as store:
        store.record_observations([dearer, cheaper], observed_at=DAY_ONE)
        store.record_changes(changes)

    with ObservationStore(path) as reopened:
        current = [
            record
            for record in reopened.get_current_observations()
            if record.product_name == "1 year"
        ]
        assert current and all(record.validation_status == "warning" for record in current)
        assert reopened.get_last_confirmed_observations() == []


def test_legacy_rate_named_migration_does_not_quarantine_an_ordinary_reprice_history(
    tmp_path, make_record
):
    """The rate is deliberately absent from identity, so a legacy product
    legitimately repriced across separate collection runs (5.59% one week,
    5.49% the next) has every one of its historical rate-named rows repair
    to the *same* identity too -- that is an ordinary reprice history, not a
    same-run collision. Grouping by repaired identity alone would quarantine
    the latest state right along with the genuine collision case above,
    leaving it with no confirmed fallback and dropping it from market
    snapshots. Only rows sharing both the repaired identity and their
    introducing run are a genuine collision."""
    path = tmp_path / "legacy.db"
    week_one = make_record(
        bank="Westpac", rate_pct=5.59, product_name="5.59% p.a.", term_label_raw="1 year"
    )
    week_two = make_record(
        bank="Westpac", rate_pct=5.49, product_name="5.49% p.a.", term_label_raw="1 year"
    )
    with ObservationStore(path) as store:
        store.record_observations([week_one], observed_at=DAY_ONE, run_id="run-week-one")
        store.record_observations([week_two], observed_at=DAY_TWO, run_id="run-week-two")

    with ObservationStore(path) as reopened:
        current = [
            record
            for record in reopened.get_current_observations()
            if record.product_name == "1 year"
        ]
        assert len(current) == 1
        assert current[0].advertised_rate_pct == 5.49
        assert current[0].validation_status != "warning"


def _build_change(make_record, run_id="run-1", **overrides):
    record = make_record()
    payload = dict(
        observation_key=record.observation_key(),
        bank=record.bank,
        product_type=ProductType.HOME_LOAN,
        product_name=record.product_name,
        rate_category=record.rate_category,
        term_months=record.term_months_normalised,
        change_type=ChangeType.RATE_DECREASE,
        detected_at=DAY_ONE,
        previous_rate_pct=4.99,
        current_rate_pct=4.79,
        change_bps=-20.0,
        run_id=run_id,
    )
    payload.update(overrides)
    return RateChange(**payload)


def test_a_change_is_not_notified_until_recorded(store, make_record):
    change = _build_change(make_record)

    assert store.was_notified(change, "telegram") is False

    store.record_notification(change, "telegram")

    assert store.was_notified(change, "telegram") is True


def test_notification_channels_are_independent(store, make_record):
    """A webhook delivery succeeding must not suppress a separate Telegram
    delivery of the same change, and vice versa."""
    change = _build_change(make_record)

    store.record_notification(change, "webhook")

    assert store.was_notified(change, "webhook") is True
    assert store.was_notified(change, "telegram") is False


def test_recording_the_same_notification_twice_is_not_an_error(store, make_record):
    change = _build_change(make_record)

    store.record_notification(change, "telegram")
    store.record_notification(change, "telegram")  # must not raise

    assert store.was_notified(change, "telegram") is True


def test_a_different_run_id_is_a_distinct_notification(store, make_record):
    """Guards delivery idempotency for one already-detected change, not
    cross-run suppression of a genuinely new event that happens to share
    every other identity field but the run it was detected in."""
    first = _build_change(make_record, run_id="run-1")
    second = _build_change(make_record, run_id="run-2")

    store.record_notification(first, "telegram")

    assert store.was_notified(first, "telegram") is True
    assert store.was_notified(second, "telegram") is False


def test_integrity_check_passes_on_a_clean_database(store, make_record):
    store.record_observations([make_record()], observed_at=DAY_ONE)

    report = store.check_integrity()

    assert report.ok is True
    assert report.issues == ()
    assert all(report.checks.values())


def test_integrity_check_flags_an_implausible_rate(store, make_record):
    store.record_observations([make_record()], observed_at=DAY_ONE)
    store._connection.execute("UPDATE observations SET rate_pct = 999")

    report = store.check_integrity()

    assert report.ok is False
    assert report.checks["rates_within_plausible_bounds"] is False
    assert any("plausible rate bounds" in issue for issue in report.issues)


def test_integrity_check_flags_a_home_loan_rate_hiding_behind_the_deposit_band(
    store, make_record
):
    """A corrupted home-loan rate must be validated against the mortgage
    band, not the widest union of mortgage and deposit bounds -- with the
    default thresholds (min_deposit_rate_pct=0.0), a 0.1% home-loan rate
    would otherwise pass because 0% is a valid deposit minimum, even though
    ordinary ingestion validation (which selects bounds by product_type)
    would reject it outright."""
    store.record_observations([make_record(product_type="home_loan")], observed_at=DAY_ONE)
    store._connection.execute("UPDATE observations SET rate_pct = 0.1")

    report = store.check_integrity()

    assert report.ok is False
    assert report.checks["rates_within_plausible_bounds"] is False
    assert any("plausible rate bounds" in issue for issue in report.issues)


def test_integrity_check_flags_an_unreadable_term_deposit_row(store, make_record):
    """get_current_observations() defaults to HOME_LOAN only -- calling it
    unscoped would silently skip term-deposit rows entirely, so a malformed
    stored deposit row (e.g. unparseable JSON in validation_warnings) would
    never surface here, and the health command could report ok=True while
    the deposit snapshot is actually unrehydratable."""
    store.record_observations(
        [make_record(product_type="term_deposit", rate_category="term_deposit")],
        observed_at=DAY_ONE,
    )
    store._connection.execute(
        "UPDATE observations SET validation_warnings = 'not-json' WHERE product_type = 'term_deposit'"
    )

    report = store.check_integrity()

    assert report.ok is False
    assert report.checks["latest_observations_readable"] is False
    assert any("unreadable" in issue for issue in report.issues)


def test_integrity_check_flags_a_broken_timestamp(store, make_record):
    store.record_observations([make_record()], observed_at=DAY_ONE)
    store._connection.execute("UPDATE observations SET last_seen_at = 'not-a-timestamp'")

    report = store.check_integrity()

    assert report.ok is False
    assert report.checks["timestamps_parse"] is False


def test_integrity_check_reports_pragma_failure(store, make_record):
    from balance_pricing_engine.pricing_intelligence.config import ValidationThresholds

    report = store.check_integrity(thresholds=ValidationThresholds())

    assert report.checks["schema_accessible"] is True


# ------------------------------------------------------ collector health


def test_update_collector_health_advances_the_streak_normally(store):
    first = store.update_collector_health(
        "ANZ", "home_loan", success=False, error="boom", at=DAY_ONE, run_id="run-1"
    )
    second = store.update_collector_health(
        "ANZ", "home_loan", success=False, error="boom", at=DAY_TWO, run_id="run-2"
    )

    assert first.consecutive_failures == 1
    assert second.consecutive_failures == 2


def test_update_collector_health_skips_advancing_for_a_retried_run_id(store):
    """A retried run_operational_cycle() invocation under the same run_id
    (e.g. crashed then re-run) must not advance the streak a second time
    for what is logically one failed collection attempt -- every other
    idempotent write in a run (record_observations, record_changes,
    notified_alerts) already treats a repeated run_id as a no-op, and an
    unguarded streak update was the one place that didn't."""
    first = store.update_collector_health(
        "ANZ", "home_loan", success=False, error="boom", at=DAY_ONE, run_id="run-1"
    )
    retried = store.update_collector_health(
        "ANZ", "home_loan", success=False, error="boom again", at=DAY_TWO, run_id="run-1"
    )

    assert first.consecutive_failures == 1
    assert retried.consecutive_failures == 1, "the same run_id must not double-count a failure"
    assert retried.last_failure_at == first.last_failure_at, (
        "a deduplicated retry must return the original, untouched state"
    )
    assert retried.last_error == "boom", "the retry's error must not overwrite the original"

    # A genuinely new run_id after the retry must still advance normally.
    third = store.update_collector_health(
        "ANZ", "home_loan", success=False, error="boom", at=DAY_THREE, run_id="run-2"
    )
    assert third.consecutive_failures == 2


def test_update_collector_health_skips_an_out_of_order_retry_of_an_older_run_id(store):
    """last_processed_run_id alone only remembers the single most recent
    run_id -- once a later, different run_id (run-B) has updated this bank,
    a delayed retry of an *earlier* run_id (run-A) no longer matches that one
    slot and would otherwise be treated as a brand new attempt, advancing
    (or resetting) the streak a second time from run-A's stale, out-of-order
    retry report even though run-A's own processing was already folded into
    the streak once."""
    run_a = store.update_collector_health(
        "ANZ", "home_loan", success=False, error="boom from A", at=DAY_ONE, run_id="run-A"
    )
    run_b = store.update_collector_health(
        "ANZ", "home_loan", success=False, error="boom from B", at=DAY_TWO, run_id="run-B"
    )
    assert run_a.consecutive_failures == 1
    assert run_b.consecutive_failures == 2

    delayed_retry_of_a = store.update_collector_health(
        "ANZ", "home_loan", success=False, error="boom from A retried", at=DAY_THREE, run_id="run-A"
    )

    assert delayed_retry_of_a.consecutive_failures == 2, (
        "a delayed retry of an already-processed (but no longer most-recent) run_id "
        "must not advance the streak again"
    )
    assert delayed_retry_of_a.last_error == "boom from B", (
        "the out-of-order retry must not overwrite run-B's already-recorded state"
    )
    assert delayed_retry_of_a.last_failure_at == run_b.last_failure_at


def test_a_pre_ledger_database_backfills_collector_health_processed_runs(store):
    """A database upgraded from before collector_health_processed_runs
    existed already has last_processed_run_id populated per bank from the
    older schema -- the migration must backfill the new ledger from it, or a
    retry of exactly the last pre-upgrade run would find the ledger empty,
    miss the already-processed lookup, and advance the streak again."""
    store.update_collector_health(
        "ANZ", "home_loan", success=False, error="boom", at=DAY_ONE, run_id="run-1"
    )

    # Simulate a database that predates the ledger table: drop it (the
    # collector_health row, including last_processed_run_id="run-1",
    # survives), then reopen (re-running _create_schema()'s migrations) as a
    # fresh ObservationStore would when it first encounters that older file.
    store._connection.execute("DROP TABLE collector_health_processed_runs")
    store._create_schema()

    retried = store.update_collector_health(
        "ANZ", "home_loan", success=False, error="boom again", at=DAY_TWO, run_id="run-1"
    )

    assert retried.consecutive_failures == 1, (
        "the backfilled ledger must recognise run-1 as already processed"
    )
    assert retried.last_error == "boom", "the retry must not overwrite the original error"


def test_update_collector_health_without_run_id_always_advances(store):
    """Omitting run_id (the default) preserves the unconditional-advance
    behaviour for a caller not tracking operational runs."""
    first = store.update_collector_health("ANZ", "home_loan", success=False, at=DAY_ONE)
    second = store.update_collector_health("ANZ", "home_loan", success=False, at=DAY_TWO)

    assert first.consecutive_failures == 1
    assert second.consecutive_failures == 2
