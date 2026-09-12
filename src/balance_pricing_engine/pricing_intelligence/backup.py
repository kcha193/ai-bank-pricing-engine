"""Lightweight SQLite backups for the pricing-intelligence observation store.

Historical pricing data cannot be regenerated -- a lost day is gone. A
timestamped copy plus simple count-based pruning is enough for this scale
(one file, daily writes); calendar-aware rotation would be overengineering
for what is, operationally, "keep the last N copies of one small file".
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3


def backup_database(
    database_path: str | Path,
    destination_dir: str | Path,
    *,
    keep: int = 30,
    now: datetime | None = None,
) -> Path:
    """Write a consistent point-in-time copy of ``database_path`` into
    ``destination_dir``, then prune older backups beyond ``keep``.

    Uses ``sqlite3.Connection.backup()`` (not a plain file copy) so a backup
    taken while the database is open elsewhere is still transactionally
    consistent, rather than risking a torn read mid-write.

    Returns the path of the backup just written.
    """
    database_path = Path(database_path)
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)

    now = now or datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    destination_path = destination_dir / f"{database_path.stem}-{timestamp}.db"
    # Written to a temporary path first, and only renamed into place once
    # source.backup() has fully succeeded: connecting sqlite3 straight to
    # destination_path creates that file immediately, before a single page
    # is copied, so a mid-backup failure (e.g. the destination disk filling
    # up) would otherwise leave an empty or truncated file sitting at the
    # real name -- list_backups() and _prune_old_backups() below both treat
    # any *.db file there as a genuine backup, so an operator could restore
    # the corrupt one, and a later successful backup could prune an older,
    # still-good copy while keeping it. The temp suffix keeps it out of both
    # functions' `*.db` glob even if cleanup below somehow doesn't run.
    temp_path = destination_path.with_name(destination_path.name + ".tmp")

    source = sqlite3.connect(str(database_path))
    try:
        try:
            destination = sqlite3.connect(str(temp_path))
            try:
                source.backup(destination)
            finally:
                destination.close()
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    finally:
        source.close()
    temp_path.rename(destination_path)

    _prune_old_backups(destination_dir, database_path.stem, keep=keep)
    return destination_path


def _prune_old_backups(destination_dir: Path, stem: str, *, keep: int) -> None:
    backups = sorted(destination_dir.glob(f"{stem}-*.db"))
    excess = len(backups) - keep
    # A plain `backups[:excess]` misbehaves when excess is negative (fewer
    # backups than `keep`): Python slicing then reads it as "all but the
    # last |excess| items" and deletes almost everything, rather than the
    # intended no-op.
    if excess > 0:
        for path in backups[:excess]:
            path.unlink()


def list_backups(destination_dir: str | Path, stem: str) -> list[Path]:
    """Existing backups for ``stem``, oldest first."""
    return sorted(Path(destination_dir).glob(f"{stem}-*.db"))
