"""Timestamped SQLite backups and count-based pruning."""

from datetime import datetime, timezone
import sqlite3

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


def test_backup_destination_is_created_if_missing(tmp_path):
    db = tmp_path / "obs.db"
    _make_db(db)
    dest = tmp_path / "does" / "not" / "exist"

    backup_path = backup_database(db, dest)

    assert backup_path.parent == dest
    assert backup_path.exists()
