"""SQLite contention behaviour for the observation store (DATA-009).

WAL lets a reader proceed beside one writer; it does not remove writer/writer contention.
These tests pin the explicit settings (WAL, bounded busy timeout), the lock-error
classification, and that a lock failure leaves no partial transaction behind.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import sqlite3
import threading
import time

import pytest

from balance_pricing_engine.competitor_rates.collectors import (
    BankCollectionResult,
    CollectionStatus,
)
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.backup import backup_database
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.sqlite_errors import (
    DATABASE_LOCKED,
    classify_sqlite_error,
    is_database_locked,
)
from balance_pricing_engine.pricing_intelligence.store import (
    DEFAULT_BUSY_TIMEOUT_SECONDS,
    MAX_BUSY_TIMEOUT_SECONDS,
    ObservationStore,
)

NOW = datetime.now(timezone.utc) - timedelta(hours=2)


def _journal_mode(path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()


# ------------------------------------------------------------ connection settings


def test_t21_a_file_backed_store_runs_in_wal_mode(tmp_path):
    path = tmp_path / "observations.db"
    with ObservationStore(path) as store:
        assert store.journal_mode == "wal"
    assert _journal_mode(path) == "wal"


def test_t22_busy_timeout_is_configured_and_bounded(tmp_path):
    with ObservationStore(tmp_path / "a.db") as store:
        busy_ms = store._connection.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy_ms == int(DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)
    with ObservationStore(tmp_path / "b.db", busy_timeout_seconds=0.25) as store:
        assert store._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 250
    for invalid in (0, -1, MAX_BUSY_TIMEOUT_SECONDS + 1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            ObservationStore(tmp_path / "c.db", busy_timeout_seconds=invalid)


def test_busy_timeout_is_a_validated_pricing_intelligence_setting():
    assert PricingIntelligenceConfig().sqlite_busy_timeout_seconds == DEFAULT_BUSY_TIMEOUT_SECONDS
    with pytest.raises(ValueError):
        PricingIntelligenceConfig(sqlite_busy_timeout_seconds=0)
    with pytest.raises(ValueError):
        PricingIntelligenceConfig(sqlite_busy_timeout_seconds=MAX_BUSY_TIMEOUT_SECONDS + 1)


def test_t24_in_memory_store_behaviour_is_preserved():
    with ObservationStore() as store:
        assert store.journal_mode == "memory"
        assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_foreign_keys_stay_enforced_in_wal_mode(tmp_path):
    with ObservationStore(tmp_path / "fk.db") as store:
        assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_t23_read_only_store_never_changes_the_source_journal_mode(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE marker (id INTEGER)")
    legacy.commit()
    legacy.close()
    assert _journal_mode(path) == "delete"

    with ObservationStore(path, read_only=True) as store:
        temp_path = store._temp_database_path
        assert store.journal_mode != "wal"
    assert _journal_mode(path) == "delete"
    assert not (tmp_path / "legacy.db-wal").exists()
    assert temp_path is not None
    for suffix in ("", "-wal", "-shm"):
        assert not os.path.exists(temp_path + suffix), f"preview copy left {suffix or 'file'}"


def test_read_only_copy_of_a_wal_database_sees_committed_wal_pages(tmp_path):
    path = tmp_path / "observations.db"
    writer = ObservationStore(path)
    writer._connection.execute("CREATE TABLE marker (id INTEGER)")
    writer._connection.execute("INSERT INTO marker VALUES (7)")
    writer._connection.commit()
    try:
        assert (tmp_path / "observations.db-wal").exists()
        with ObservationStore(path, read_only=True) as preview:
            assert preview._connection.execute("SELECT id FROM marker").fetchall()[0][0] == 7
    finally:
        writer.close()


def test_backup_of_a_wal_database_is_a_self_contained_single_file(tmp_path):
    path = tmp_path / "observations.db"
    store = ObservationStore(path)
    store._connection.execute("CREATE TABLE marker (id INTEGER)")
    store._connection.execute("INSERT INTO marker VALUES (11)")
    store._connection.commit()
    try:
        backup = backup_database(path, tmp_path / "backups")
    finally:
        store.close()
    assert _journal_mode(backup) == "delete"
    assert not backup.with_name(backup.name + "-wal").exists()
    connection = sqlite3.connect(backup)
    try:
        assert connection.execute("SELECT id FROM marker").fetchone()[0] == 11
    finally:
        connection.close()


# ------------------------------------------------------- concurrent access


def test_t25_a_reader_is_not_blocked_by_an_open_writer_and_sees_committed_state(tmp_path):
    path = tmp_path / "observations.db"
    with ObservationStore(path) as store:
        store._connection.execute("CREATE TABLE marker (id INTEGER)")
        store._connection.execute("INSERT INTO marker VALUES (1)")
        store._connection.commit()

    # Opened first: opening a store runs its idempotent schema check under the write lock
    # (and so waits on the busy timeout like any writer); reading never does.
    reader = ObservationStore(path, busy_timeout_seconds=5)
    writer = sqlite3.connect(path, timeout=0)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO marker VALUES (2)")
    try:
        started = time.monotonic()
        rows = reader._connection.execute("SELECT id FROM marker ORDER BY id").fetchall()
        assert [row[0] for row in rows] == [1]
        assert time.monotonic() - started < 1
        writer.commit()
        rows = reader._connection.execute("SELECT id FROM marker ORDER BY id").fetchall()
        assert [row[0] for row in rows] == [1, 2]
    finally:
        writer.close()
        reader.close()


def test_a_second_writer_waits_within_the_busy_timeout_then_succeeds(tmp_path):
    path = tmp_path / "observations.db"
    with ObservationStore(path) as store:
        store._connection.execute("CREATE TABLE marker (id INTEGER)")
        store._connection.commit()

    store = ObservationStore(path, busy_timeout_seconds=5)
    blocker = sqlite3.connect(path, timeout=0, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    releaser = threading.Timer(0.3, blocker.rollback)
    releaser.start()
    try:
        started = time.monotonic()
        with store._transaction() as connection:
            connection.execute("INSERT INTO marker VALUES (3)")
        assert time.monotonic() - started >= 0.25, "the writer must have waited for the lock"
        assert store._connection.execute("SELECT count(*) FROM marker").fetchone()[0] == 1
    finally:
        releaser.join()
        blocker.close()
        store.close()


def test_t26_writer_contention_beyond_the_busy_timeout_is_classified(tmp_path):
    path = tmp_path / "observations.db"
    with ObservationStore(path) as store:
        store._connection.execute("CREATE TABLE marker (id INTEGER)")
        store._connection.commit()

    store = ObservationStore(path, busy_timeout_seconds=0.2)
    blocker = sqlite3.connect(path, timeout=0)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with store:
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError) as caught:
                with store._transaction() as connection:
                    connection.execute("INSERT INTO marker VALUES (4)")
            elapsed = time.monotonic() - started
        assert is_database_locked(caught.value)
        assert classify_sqlite_error(caught.value) == DATABASE_LOCKED
        assert 0.15 <= elapsed < 5, "the wait must be bounded by the busy timeout"
    finally:
        blocker.rollback()
        blocker.close()


# --------------------------------------------------------------- classification


@pytest.mark.parametrize(
    "message",
    ["database is locked", "database table is locked", "database schema is locked: main"],
)
def test_lock_messages_are_classified_as_database_locked(message):
    assert classify_sqlite_error(sqlite3.OperationalError(message)) == DATABASE_LOCKED


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("database or disk is full"),
        sqlite3.OperationalError("attempt to write a readonly database"),
        sqlite3.OperationalError("unable to open database file"),
        sqlite3.OperationalError("no such table: observations"),
        sqlite3.OperationalError('near "SELEC": syntax error'),
        sqlite3.OperationalError("disk I/O error"),
        sqlite3.DatabaseError("database disk image is malformed"),
        sqlite3.DatabaseError("file is not a database"),
        sqlite3.IntegrityError("FOREIGN KEY constraint failed"),
        RuntimeError("database is locked"),
    ],
)
def test_other_sqlite_failures_are_not_mistaken_for_a_lock(error):
    assert not is_database_locked(error)
    assert classify_sqlite_error(error) != DATABASE_LOCKED


def test_real_non_lock_errors_are_not_classified_as_locks(tmp_path):
    connection = sqlite3.connect(tmp_path / "x.db")
    with pytest.raises(sqlite3.OperationalError) as missing_table:
        connection.execute("SELECT * FROM nowhere")
    connection.close()
    assert not is_database_locked(missing_table.value)

    bogus = tmp_path / "not-a-db.db"
    bogus.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(sqlite3.DatabaseError) as corrupt:
        ObservationStore(bogus)
    assert not is_database_locked(corrupt.value)


# ------------------------------------------ lock during an operational write


class _BlockingStubCollector:
    """Succeeds, then lets a test grab the write lock before the store write happens."""

    def __init__(self, bank, record, on_collect):
        self.bank = bank
        self._record = record
        self._on_collect = on_collect

    def collect_mortgage_rates(self, captured_at_utc=None, persist_snapshot=True):
        self._on_collect()
        return BankCollectionResult(
            bank=self.bank,
            product_type=ProductType.HOME_LOAN,
            status=CollectionStatus.SUCCESS,
            source_url="https://www.example-bank.co.nz/rates",
            records=(self._record.model_copy(update={"captured_at_utc": NOW}),),
        )


def _counts(path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("observations", "rate_changes", "collection_runs", "operational_runs")
        }
    finally:
        connection.close()


def test_t28_t29_a_lock_during_the_write_leaves_no_partial_transaction(tmp_path, make_record):
    path = tmp_path / "observations.db"
    record = make_record(bank="ANZ")
    blocker_holder: dict[str, sqlite3.Connection] = {}

    def grab_lock():
        blocker = sqlite3.connect(path, timeout=0)
        blocker.execute("BEGIN IMMEDIATE")
        blocker_holder["connection"] = blocker

    config = PricingIntelligenceConfig(
        database_path=str(path),
        selected_banks=["ANZ"],
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(path),
        collectors=[_BlockingStubCollector("ANZ", record, lambda: None)],
    ) as service:
        run_operational_cycle(service, captured_at_utc=NOW - timedelta(hours=1), alert_sinks={})
    before = _counts(path)
    assert before["observations"] >= 1

    moved = record.model_copy(update={"advertised_rate_pct": record.advertised_rate_pct + 0.25})
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(path, busy_timeout_seconds=0.2),
        collectors=[_BlockingStubCollector("ANZ", moved, grab_lock)],
    ) as service:
        try:
            with pytest.raises(sqlite3.OperationalError) as caught:
                run_operational_cycle(service, captured_at_utc=NOW, alert_sinks={})
        finally:
            blocker_holder["connection"].rollback()
            blocker_holder["connection"].close()
    assert classify_sqlite_error(caught.value) == DATABASE_LOCKED
    assert _counts(path) == before
    with ObservationStore(path) as store:
        assert store._connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
