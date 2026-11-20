"""Transactional content provenance, independent of SQLite's journal/checkpoint lifecycle.

Every application table gets row triggers. The singleton token changes inside the same
transaction as its data, so other connections see neither uncommitted nor rolled-back
revisions. Readers validate trigger coverage and fail closed on unmigrated/unknown schemas.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import logging
from pathlib import Path
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

_TABLE = "content_revision"
_EVENTS = ("INSERT", "UPDATE", "DELETE")


@dataclass(frozen=True)
class DatabaseStamp:
    """Versioned committed-content identity; filesystem timestamps are not provenance."""

    path: str
    device: int
    inode: int
    schema_version: int
    revision: str
    format_version: int = 1


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND substr(name, 1, 7) != 'sqlite_' AND name != ?",
            (_TABLE,),
        )
    ]


def _trigger_sql(table: str, event: str) -> str:
    name = _identifier(f"content_revision_{table}_{event.lower()}")
    return (
        f"CREATE TRIGGER {name} AFTER {event} ON {_identifier(table)} BEGIN "
        "UPDATE content_revision SET token = lower(hex(randomblob(16))) WHERE singleton = 1; END"
    )


def install_revision_tracking(connection: sqlite3.Connection) -> None:
    """Install after all store migrations, inside their transaction; reopening is inert.

    Enumerating tables rather than Python write methods includes quarantine helpers, direct
    SQL, UPDATEs of existing observations, and future tables. sqlite_* bookkeeping tables
    are excluded; their owning application mutations already fire these triggers.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS content_revision ("
        "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), token TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT OR IGNORE INTO content_revision VALUES (1, lower(hex(randomblob(16))))"
    )
    for table in _tables(connection):
        for event in _EVENTS:
            connection.execute(
                _trigger_sql(table, event).replace(
                    "CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ", 1
                )
            )


def read_database_stamp(database_path: str | Path) -> Optional[DatabaseStamp]:
    """Read committed provenance in one short, read-only transaction, or return unknown.

    No ObservationStore construction, migrations, checkpoint or persistent connection.
    A missing file is never created. SQLite owns WAL locking/visibility, including same-size
    reuse. The 100 ms lock bound keeps a busy/corrupt/unreadable store from blocking a render.
    """
    path = Path(database_path).resolve()
    try:
        before = path.stat()
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.1)) as reader:
            reader.execute("BEGIN")
            token = reader.execute(
                "SELECT token FROM content_revision WHERE singleton = 1"
            ).fetchone()
            if token is None or not isinstance(token[0], str) or len(token[0]) != 32:
                return None
            triggers = {
                row[0]
                for row in reader.execute("SELECT sql FROM sqlite_master WHERE type='trigger'")
            }
            if any(
                _trigger_sql(table, event) not in triggers
                for table in _tables(reader)
                for event in _EVENTS
            ):
                return None
            schema_version = reader.execute("PRAGMA schema_version").fetchone()[0]
            after = path.stat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                return None
            return DatabaseStamp(str(path), after.st_dev, after.st_ino, schema_version, token[0])
    except (OSError, sqlite3.Error):
        # Unknown provenance is a safety outcome, never an unchanged/missing-file cache key.
        logger.warning("Database provenance unavailable; stored scenario results must be hidden")
        return None
