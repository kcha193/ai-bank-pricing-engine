"""Timestamped SQLite backups and count-based pruning."""

from datetime import datetime, timezone
import sqlite3

import pytest

from balance_pricing_engine.pricing_intelligence.backup import backup_database, list_backups


def _make_db(path, value=1):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.commit()
    conn.close()


def test_backup_creates_a_consistent_copy(tmp_path):
    db = tmp_path / "obs.db"
    _make_db(db, value=42)

    backup_path = backup_database(db, tmp_path / "backups", now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert backup_path.exists()
    conn = sqlite3.connect(str(backup_path))
    assert conn.execute("SELECT x FROM t").fetchone() == (42,)


def test_backup_filename_includes_a_timestamp(tmp_path):
    db = tmp_path / "obs.db"
    _make_db(db)

    backup_path = backup_database(db, tmp_path / "backups", now=datetime(2026, 3, 14, 9, 30, tzinfo=timezone.utc))

    assert backup_path.name == "obs-20260314T093000Z.db"


def test_pruning_keeps_only_the_most_recent_n(tmp_path):
    db = tmp_path / "obs.db"
    _make_db(db)
    dest = tmp_path / "backups"

    for day in range(1, 6):
        backup_database(db, dest, keep=3, now=datetime(2026, 1, day, tzinfo=timezone.utc))

    backups = list_backups(dest, "obs")
    assert len(backups) == 3
    assert backups[0].name == "obs-20260103T000000Z.db"
    assert backups[-1].name == "obs-20260105T000000Z.db"


class _FailingSourceConnection(sqlite3.Connection):
    """sqlite3.Connection is a C-extension type and cannot be monkeypatched
    directly (setting an attribute on it raises TypeError) -- subclassing
    and injecting via sqlite3.connect(..., factory=...) is the supported way
    to override one method's behaviour for a test."""

    def backup(self, *args, **kwargs):
        raise sqlite3.OperationalError("simulated disk full")


def _patch_source_connect_to_fail(monkeypatch, backup_module, db_path):
    """Make backup_database()'s *source* connection (opened against
    db_path) fail its own .backup() call, while every other sqlite3.connect
    call (the temp destination, this test's own setup/assertions) behaves
    normally."""
    real_connect = sqlite3.connect

    def fake_connect(path, *args, **kwargs):
        if str(path) == str(db_path):
            return real_connect(path, *args, factory=_FailingSourceConnection, **kwargs)
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(backup_module.sqlite3, "connect", fake_connect)


def test_a_failed_backup_does_not_leave_a_corrupt_file_at_the_destination(
    tmp_path, monkeypatch
):
    """A mid-backup failure (e.g. the destination disk filling up) must not
    leave an empty or partial file sitting at the real backup name --
    list_backups() and the retention pass would otherwise treat it as a
    genuine backup, letting an operator restore it and a later successful
    backup prune an older, still-good copy while keeping the corrupt one."""
    from balance_pricing_engine.pricing_intelligence import backup as backup_module

    db = tmp_path / "obs.db"
    _make_db(db)
    dest = tmp_path / "backups"

    _patch_source_connect_to_fail(monkeypatch, backup_module, db)

    with pytest.raises(sqlite3.OperationalError):
        backup_database(db, dest, now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert list_backups(dest, "obs") == []
    assert list(dest.glob("*")) == [], "no partial backup or temp file may be left behind"


def test_a_later_successful_backup_still_prunes_correctly_after_a_failure(
    tmp_path, monkeypatch
):
    """A failed attempt must not linger and confuse the next, successful
    backup's own retention count."""
    from balance_pricing_engine.pricing_intelligence import backup as backup_module

    db = tmp_path / "obs.db"
    _make_db(db)
    dest = tmp_path / "backups"

    _patch_source_connect_to_fail(monkeypatch, backup_module, db)
    with pytest.raises(sqlite3.OperationalError):
        backup_database(db, dest, keep=1, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    monkeypatch.undo()

    backup_database(db, dest, keep=1, now=datetime(2026, 1, 2, tzinfo=timezone.utc))

    backups = list_backups(dest, "obs")
    assert len(backups) == 1
    assert backups[0].name == "obs-20260102T000000Z.db"


def test_backup_refuses_a_missing_source_database(tmp_path):
    """sqlite3.connect() silently creates a new, empty database file for a
    nonexistent path instead of failing -- without this guard,
    backup_database() would "successfully" publish that empty database as a
    backup and prune older, still-good copies via its mere presence. Must
    raise before ever touching the destination directory or sqlite3."""
    db = tmp_path / "does-not-exist.db"
    dest = tmp_path / "backups"

    with pytest.raises(FileNotFoundError):
        backup_database(db, dest)

    assert not dest.exists(), "must fail before creating the destination directory"
    assert not db.exists(), "must not have created an empty database at the missing source path"


def test_backup_destination_is_created_if_missing(tmp_path):
    db = tmp_path / "obs.db"
    _make_db(db)
    dest = tmp_path / "does" / "not" / "exist"

    backup_path = backup_database(db, dest)

    assert backup_path.parent == dest
    assert backup_path.exists()
