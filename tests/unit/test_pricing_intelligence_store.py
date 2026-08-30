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
    store.record_observations([deposit_record], run_id="legacy-run", observed_at=DAY_ONE)

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
        [bnz_record], run_id="run-returned", observed_at=DAY_ONE
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
    # INSERT OR REPLACE on the same run_id overwrites collection_runs'
    # stored product_type to term_deposit here -- exactly the ambiguity a
    # genuinely mixed legacy run leaves behind for the home-loan side.
    store.record_observations(
        [make_record(product_type=ProductType.TERM_DEPOSIT, rate_category="term_deposit")],
        run_id=shared_run_id,
        observed_at=DAY_ONE,
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
    store.record_observations([deposit_record], run_id=shared_run_id, observed_at=DAY_ONE)
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
    store.record_observations(
        [make_record(product_type=ProductType.HOME_LOAN)],
        run_id=shared_run_id,
        observed_at=DAY_ONE,
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
