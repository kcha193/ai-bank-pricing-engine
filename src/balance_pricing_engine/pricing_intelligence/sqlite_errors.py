"""Classification of SQLite failures for operational reporting (DATA-009).

Only lock contention -- ``SQLITE_BUSY`` / ``SQLITE_LOCKED`` and their extended codes,
surfaced by Python as "database is locked", "database table is locked" or "database
schema is locked" -- is classified as ``database_locked``. That condition is transient:
another writer (the other product's run, a backup, a manual session) holds the write lock
past the busy timeout, and a later run can succeed unchanged.

Every other SQLite failure keeps its own identity: corruption, a full disk, a read-only or
missing file, a schema mismatch or invalid SQL each needs a different operator response,
and reporting any of them as "locked" would send the operator to wait out a fault that
waiting cannot fix.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

DATABASE_LOCKED = "database_locked"

#: Primary result codes (low byte of an extended code): SQLITE_BUSY=5, SQLITE_LOCKED=6.
_LOCK_PRIMARY_CODES = frozenset({5, 6})
_LOCK_MESSAGES = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)


def is_database_locked(error: BaseException) -> bool:
    """True only for SQLite busy/locked contention."""
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code: Optional[int] = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in _LOCK_PRIMARY_CODES
    message = str(error).lower()
    return any(marker in message for marker in _LOCK_MESSAGES)


def classify_sqlite_error(error: BaseException) -> Optional[str]:
    """Stable ``error_type`` for a SQLite failure: ``database_locked`` for contention,
    otherwise the exception class name (``OperationalError``, ``DatabaseError``, ...)."""
    if is_database_locked(error):
        return DATABASE_LOCKED
    if isinstance(error, sqlite3.Error):
        return type(error).__name__
    return None
