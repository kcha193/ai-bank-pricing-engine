"""Committed SQLite changes invalidate dashboard provenance, independent of checkpointing."""

from datetime import datetime, timezone
import sqlite3

from balance_pricing_engine.dashboard.freshness import ResultFreshness, assess_result
from balance_pricing_engine.dashboard.models import ScenarioRequest
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _services(path):
    return DashboardServices(load_settings({}, database_path=str(path)))


def test_open_writer_wal_commit_changes_stamp_and_invalidates_result(tmp_path, make_record):
    path = tmp_path / "market.db"
    with ObservationStore(path) as writer:
        writer._connection.execute("PRAGMA wal_autocheckpoint=0")
        writer.record_observations([make_record(rate_pct=4.89)], run_id="before", observed_at=NOW)
        writer._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        services = _services(path)
        before = services.db_stamp()
        main_mtime = path.stat().st_mtime_ns
        request = ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None)
        stored = (request, object(), before, NOW, services.config_fingerprint())
        writer.record_observations([make_record(rate_pct=4.79)], run_id="after", observed_at=NOW)
        reader = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            assert reader.execute("SELECT count(*) FROM collection_runs").fetchone()[0] == 2
        finally:
            reader.close()
        assert path.stat().st_mtime_ns == main_mtime
        after = services.db_stamp()
        assert after != before, "committed WAL data must invalidate the dashboard stamp"
        assert (
            assess_result(
                stored,
                mode="home_loan",
                request=request,
                db_stamp=after,
                config_fingerprint=services.config_fingerprint(),
                terms_available=True,
            ).status
            is ResultFreshness.MARKET_DATA_CHANGED
        )


def test_uncommitted_and_rolled_back_changes_keep_stamp(tmp_path, make_record):
    path = tmp_path / "market.db"
    with ObservationStore(path) as writer:
        writer.record_observations([make_record()], run_id="initial", observed_at=NOW)
        services = _services(path)
        stamp = services.db_stamp()
        writer._connection.execute("UPDATE observations SET rate_pct=9")
        assert services.db_stamp() == stamp
        writer._connection.rollback()
        assert services.db_stamp() == stamp


def test_repeated_wal_commits_and_same_size_reuse(tmp_path, make_record):
    path = tmp_path / "market.db"
    with ObservationStore(path) as writer:
        writer.record_observations([make_record()], run_id="initial", observed_at=NOW)
        writer._connection.execute("PRAGMA wal_autocheckpoint=0")
        writer._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        services = _services(path)
        stamps = [services.db_stamp()]
        for value in (4.71, 4.72, 4.73):
            writer._connection.execute("UPDATE observations SET rate_pct=?", (value,))
            writer._connection.commit()
            stamps.append(services.db_stamp())
        assert len(set(stamps)) == len(stamps)
        wal = path.with_name(path.name + "-wal")
        size = wal.stat().st_size
        writer._connection.execute("PRAGMA wal_checkpoint(RESTART)")
        checkpoint_stamp = services.db_stamp()
        writer._connection.execute("UPDATE observations SET rate_pct=4.74")
        writer._connection.commit()
        assert wal.stat().st_size == size
        assert services.db_stamp() != checkpoint_stamp
        final = services.db_stamp()
        writer._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert services.db_stamp() == final  # checkpoint is not new market content
    assert not wal.exists()
    assert services.db_stamp() == final


def test_stamp_handles_removal_recreation_and_independent_paths(tmp_path):
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    services = _services(first)
    assert services.db_stamp() is None
    assert not first.exists()
    with ObservationStore(first):
        initial = services.db_stamp()
        assert initial is not None
    with ObservationStore(second):
        assert _services(second).db_stamp() != initial
    first.unlink()
    assert services.db_stamp() is None
    with ObservationStore(first):
        assert services.db_stamp() != initial


def test_rollback_journal_commits_invalidate_stamp(tmp_path, make_record):
    path = tmp_path / "market.db"
    with ObservationStore(path) as writer:
        writer.record_observations([make_record()], run_id="initial", observed_at=NOW)
        assert writer._connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        services = _services(path)
        before = services.db_stamp()
        writer._connection.execute("UPDATE observations SET rate_pct=4.1")
        assert services.db_stamp() == before
        writer._connection.rollback()
        assert services.db_stamp() == before
        writer._connection.execute("UPDATE observations SET rate_pct=4.2")
        writer._connection.commit()
        assert services.db_stamp() != before


def test_unknown_database_never_creates_schema_or_returns_provenance(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE old (value)")
    connection.close()
    before = path.read_bytes()
    assert _services(path).db_stamp() is None
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_stamp_queries_are_read_only_and_preserve_database(tmp_path):
    path = tmp_path / "market.db"
    with ObservationStore(path) as writer:
        writer._connection.execute("PRAGMA journal_mode=DELETE")
    before = path.read_bytes()
    path.chmod(0o444)
    try:
        stamps = [_services(path).db_stamp() for _ in range(3)]
        assert stamps[0] is not None and len(set(stamps)) == 1
        assert path.read_bytes() == before
        assert list(tmp_path.iterdir()) == [path]
    finally:
        path.chmod(0o644)


def test_every_store_table_has_transactional_mutation_coverage(tmp_path):
    path = tmp_path / "market.db"
    with ObservationStore(path) as writer:
        tables = {
            row[0]
            for row in writer._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        triggers = list(
            writer._connection.execute(
                "SELECT tbl_name, sql FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'content_revision_%'"
            )
        )
        for table in tables - {"content_revision"}:
            for event in ("INSERT", "UPDATE", "DELETE"):
                assert any(row[0] == table and f"AFTER {event}" in row[1] for row in triggers)


def test_new_untracked_table_or_removed_trigger_fails_closed(tmp_path):
    path = tmp_path / "market.db"
    with ObservationStore(path) as writer:
        services = _services(path)
        assert services.db_stamp() is not None
        writer._connection.execute("CREATE TABLE future_input (value)")
        writer._connection.commit()
        assert services.db_stamp() is None
    with ObservationStore(path) as writer:
        assert services.db_stamp() is not None
        writer._connection.execute("DROP TRIGGER content_revision_observations_update")
        writer._connection.commit()
        assert services.db_stamp() is None


def test_wal_coordination_is_allowed_but_stamp_never_changes_data_or_schema(tmp_path):
    path = tmp_path / "market.db"
    with ObservationStore(path):
        pass
    before = path.read_bytes()
    services = _services(path)
    first = services.db_stamp()
    assert first is not None
    assert services.db_stamp() == first
    assert path.read_bytes() == before
    # SQLite may create WAL/SHM coordination files for a read-only WAL connection.
    assert {p.name for p in tmp_path.iterdir()} <= {"market.db", "market.db-wal", "market.db-shm"}


def test_reopening_a_store_does_not_change_committed_provenance(tmp_path):
    path = tmp_path / "market.db"
    with ObservationStore(path):
        pass
    before = _services(path).db_stamp()
    with ObservationStore(path):
        assert _services(path).db_stamp() == before
    assert _services(path).db_stamp() == before


def test_legacy_database_is_upgraded_by_writer_not_stamp_reads(tmp_path):
    path = tmp_path / "legacy.db"
    with ObservationStore(path) as writer:
        for row in writer._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'content_revision_%'"
        ).fetchall():
            writer._connection.execute(f'DROP TRIGGER "{row[0]}"')
        writer._connection.execute("DROP TABLE content_revision")
        writer._connection.commit()
    assert _services(path).db_stamp() is None
    with ObservationStore(path):
        assert _services(path).db_stamp() is not None


def test_invalid_or_missing_revision_token_fails_closed(tmp_path):
    path = tmp_path / "market.db"
    with ObservationStore(path) as writer:
        services = _services(path)
        writer._connection.execute("UPDATE content_revision SET token='bad'")
        writer._connection.commit()
        assert services.db_stamp() is None
        writer._connection.execute("DELETE FROM content_revision")
        writer._connection.commit()
        assert services.db_stamp() is None


def test_altered_trigger_definition_fails_closed(tmp_path):
    path = tmp_path / "market.db"
    with ObservationStore(path) as writer:
        writer._connection.execute("DROP TRIGGER content_revision_observations_update")
        writer._connection.execute(
            "CREATE TRIGGER content_revision_observations_update AFTER UPDATE ON observations "
            "BEGIN SELECT 1; END"
        )
        writer._connection.commit()
        assert _services(path).db_stamp() is None


def test_database_replacement_during_stamp_read_is_unknown(tmp_path, monkeypatch):
    import balance_pricing_engine.pricing_intelligence.revision as revision

    path, replacement = tmp_path / "market.db", tmp_path / "replacement.db"
    for file in (path, replacement):
        with ObservationStore(file) as writer:
            writer._connection.execute("PRAGMA journal_mode=DELETE")
    connect = sqlite3.connect

    def replace_after_open(*args, **kwargs):
        reader = connect(*args, **kwargs)
        replacement.replace(path)
        return reader

    monkeypatch.setattr(revision.sqlite3, "connect", replace_after_open)
    assert _services(path).db_stamp() is None


def test_direct_sql_insert_update_delete_are_all_tracked(tmp_path, make_record):
    path = tmp_path / "market.db"
    with ObservationStore(path) as writer:
        services = _services(path)
        before = services.db_stamp()
        writer.record_observations([make_record()], run_id="initial", observed_at=NOW)
        inserted = services.db_stamp()
        writer._connection.execute("UPDATE observations SET rate_pct=4.5")
        writer._connection.commit()
        updated = services.db_stamp()
        writer._connection.execute("DELETE FROM observations")
        writer._connection.commit()
        deleted = services.db_stamp()
        assert len({before, inserted, updated, deleted}) == 4


def test_dashboard_data_read_never_migrates_a_legacy_database(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE legacy_input (value)")
    before = path.read_bytes()
    _services(path).header()
    assert path.read_bytes() == before
    with sqlite3.connect(path) as reader:
        assert reader.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("legacy_input",)
        ]


def test_live_reader_sees_wal_commits_but_rejects_mutation(tmp_path, make_record):
    import pytest

    path = tmp_path / "market.db"
    with ObservationStore(path) as writer:
        writer.record_observations([make_record()], run_id="initial", observed_at=NOW)
        with ObservationStore.open_reader(path) as reader:
            assert len(reader.get_current_observations()) == 1
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader._connection.execute("DELETE FROM observations")
    missing = tmp_path / "absent" / "market.db"
    with pytest.raises(sqlite3.OperationalError):
        ObservationStore.open_reader(missing)
    assert not missing.parent.exists()


def test_dashboard_cache_invalidates_on_commits_and_bypasses_unknown_stamps(
    market_factory, monkeypatch
):
    from dashboard_harness import make_services
    from balance_pricing_engine.dashboard.views._common import cached, _cached

    market = market_factory()
    services = make_services(market)
    _cached.clear()
    with ObservationStore(market.database) as writer:
        assert cached(services, "term_summary", 12).value.lowest_pct == 4.79
        writer._connection.execute("UPDATE observations SET rate_pct=4.20")
        writer._connection.commit()
        assert cached(services, "term_summary", 12).value.lowest_pct == 4.20
        monkeypatch.setattr(services, "db_stamp", lambda: None)
        assert cached(services, "term_summary", 12).value.lowest_pct == 4.20
        writer._connection.execute("UPDATE observations SET rate_pct=4.10")
        writer._connection.commit()
        assert cached(services, "term_summary", 12).value.lowest_pct == 4.10
    _cached.clear()
