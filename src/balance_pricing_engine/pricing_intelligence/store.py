"""Historical observation store for public NZ bank pricing.

Storage choice: **SQLite**, from the standard library. The repository had no
existing persistence layer, and SQLite adds no dependency, needs no server, and
handles the query shapes this layer needs (latest-per-key, per-key history,
changes in a date window) at the scale of five banks x seven terms x daily
collection. DuckDB would be the better pick only if this grew into large
analytical scans; it would add a dependency for no present benefit.

Business logic never sees SQL: this module maps rows to and from
:class:`CompetitorRateRecord` -- the same canonical model the collectors emit --
and everything downstream (change detection, snapshots, positioning) works on
those objects.

**Slowly-changing storage.** A daily collection where nothing moved must not
create meaningless history. Each row represents one *distinct advertised state*
of a rate line: an unchanged re-observation bumps ``last_seen_at`` and
``observation_count`` on the existing row instead of inserting a duplicate, and
a material change inserts a new row. History therefore reads as the sequence of
states a rate actually took.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import re
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Iterable, Iterator, Mapping, NamedTuple, Optional, Sequence
import uuid

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.changes import ChangeType, RateChange
from balance_pricing_engine.pricing_intelligence.config import ValidationThresholds

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Uniqueness is scoped to run_id (not just observation_key/change_type/
#: detected_at) so a genuinely new event from a later run is never silently
#: dropped just because an earlier run's event happened to share the exact
#: same detected_at -- e.g. four runs sharing one started_at (observed,
#: withdrawn, returned, withdrawn again) each get their own REMOVED_PRODUCT
#: row instead of the second withdrawal colliding with the first's. Shared
#: with :meth:`ObservationStore._migrate_rate_changes_run_scoped_uniqueness`,
#: which recreates this table with the same definition for a database
#: created before run_id joined the constraint.
_RATE_CHANGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_changes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_key     TEXT NOT NULL,
    bank                TEXT NOT NULL,
    product_type        TEXT NOT NULL,
    product_name        TEXT NOT NULL,
    rate_category       TEXT NOT NULL,
    term_months         INTEGER,
    change_type         TEXT NOT NULL,
    previous_rate_pct   REAL,
    current_rate_pct    REAL,
    change_bps          REAL,
    previous_observed_at TEXT,
    detected_at         TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    suspicious          INTEGER NOT NULL DEFAULT 0,
    detail              TEXT,
    source_url          TEXT,
    UNIQUE (observation_key, change_type, detected_at, run_id)
);

CREATE INDEX IF NOT EXISTS idx_changes_detected_at ON rate_changes (detected_at);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id              TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL,
    banks               TEXT NOT NULL,
    product_type        TEXT NOT NULL DEFAULT 'home_loan',
    record_count        INTEGER NOT NULL DEFAULT 0,
    inserted_count      INTEGER NOT NULL DEFAULT 0,
    unchanged_count     INTEGER NOT NULL DEFAULT 0,
    report_json         TEXT
);

-- A durable, write-once ledger of which (run_id, product_type) pairs
-- record_observations() has actually processed -- the sole source of truth
-- for its idempotent-retry short-circuit, and (via its own started_at/rowid)
-- for per-product run ordering in latest_run_id()/_previous_run_row().
-- collection_runs.product_type/started_at alone cannot serve either: they
-- hold only one value each, so a legacy run_id reused for a second product
-- (two calls sharing one run_id, one per product, each with its own
-- observed_at) overwrites both -- retroactively changing the *first*
-- product's recorded start time to the second call's, not just losing which
-- product owns the label. Falling back to observations.last_seen_run_id/
-- first_seen_run_id or rate_changes.run_id (each paired with product_type)
-- isn't reliable either: a quiet reconfirmation-only call leaves no
-- rate_changes row at all, and last_seen_run_id/first_seen_run_id can each
-- move on to a later run, erasing the only trace this run_id ever touched
-- that product. This table is never updated after its row is first
-- written, so both traces -- that the pair happened, and when -- survive
-- regardless of what any later call (same run_id, other product; or a
-- separate, later run) subsequently does to collection_runs/observations/
-- rate_changes.
CREATE TABLE IF NOT EXISTS collection_run_products (
    run_id              TEXT NOT NULL,
    product_type        TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    inserted_count      INTEGER NOT NULL DEFAULT 0,
    unchanged_count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, product_type)
);

CREATE TABLE IF NOT EXISTS observations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_key     TEXT NOT NULL,
    bank                TEXT NOT NULL,
    product_type        TEXT NOT NULL,
    product_name        TEXT NOT NULL,
    rate_category       TEXT NOT NULL,
    term_months         INTEGER,
    term_label_raw      TEXT NOT NULL,
    term_days_raw       INTEGER,
    rate_pct            REAL NOT NULL,
    interest_payment_frequency TEXT NOT NULL DEFAULT 'unknown',
    min_balance         REAL,
    max_balance         REAL,
    lvr_condition       TEXT,
    max_lvr             REAL,
    min_equity          REAL,
    new_business_only   INTEGER,
    cashback_text       TEXT,
    promotion_text      TEXT,
    package_requirement TEXT,
    eligibility_text    TEXT,
    source_url          TEXT NOT NULL,
    source_page_title   TEXT,
    source_text         TEXT NOT NULL,
    effective_date_text TEXT,
    content_hash        TEXT NOT NULL,
    parser_version      TEXT NOT NULL,
    validation_status   TEXT NOT NULL DEFAULT 'unvalidated',
    validation_warnings TEXT NOT NULL DEFAULT '[]',
    first_observed_at   TEXT NOT NULL,
    first_seen_run_id   TEXT,
    last_seen_at        TEXT NOT NULL,
    last_seen_run_id    TEXT NOT NULL,
    observation_count   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_observations_key
    ON observations (observation_key, first_observed_at);
CREATE INDEX IF NOT EXISTS idx_observations_bank_term
    ON observations (bank, product_type, term_months);
CREATE INDEX IF NOT EXISTS idx_observations_last_seen
    ON observations (last_seen_run_id);

-- Idempotency ledger for alert *delivery* (not detection -- detect_changes()
-- itself only ever fires a given transition once, since an unchanged
-- re-observation never re-inserts a rate_changes row; see the module
-- docstring). This exists for the narrower case of re-processing the same
-- already-detected change -- a crashed run retried under the same run_id, or
-- a future manual "resend recent alerts" tool -- which must not re-notify a
-- channel that already delivered it. Keyed on the same identity
-- rate_changes' own UNIQUE constraint uses, plus the channel, since Telegram
-- and a webhook are independent deliveries and one succeeding must not
-- suppress the other.
CREATE TABLE IF NOT EXISTS notified_alerts (
    observation_key     TEXT NOT NULL,
    change_type         TEXT NOT NULL,
    detected_at         TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    channel             TEXT NOT NULL,
    notified_at         TEXT NOT NULL,
    PRIMARY KEY (observation_key, change_type, detected_at, run_id, channel)
);

-- One row per run_operational_cycle() invocation -- see RunRecord's
-- docstring for why this is a separate table from collection_runs rather
-- than additional columns on it.
CREATE TABLE IF NOT EXISTS operational_runs (
    run_id                   TEXT PRIMARY KEY,
    started_at               TEXT NOT NULL,
    completed_at             TEXT,
    status                   TEXT NOT NULL,
    product_type             TEXT NOT NULL,
    banks_attempted          TEXT NOT NULL,
    banks_successful         TEXT NOT NULL,
    banks_failed             TEXT NOT NULL,
    rates_observed           INTEGER NOT NULL DEFAULT 0,
    rates_changed            INTEGER NOT NULL DEFAULT 0,
    validation_failures      INTEGER NOT NULL DEFAULT 0,
    suspicious_observations  INTEGER NOT NULL DEFAULT 0,
    market_state             TEXT,
    error_summary            TEXT,
    collection_run_id        TEXT
);

CREATE INDEX IF NOT EXISTS idx_operational_runs_started_at
    ON operational_runs (product_type, started_at);

-- Consecutive-failure streak per bank/product, surviving across scheduler
-- process restarts (each scheduled run is a separate process). Updated once
-- per bank per run_operational_cycle() call; see
-- orchestration.classify_health_escalation() for the alerting policy this
-- drives.
CREATE TABLE IF NOT EXISTS collector_health (
    bank                   TEXT NOT NULL,
    product_type           TEXT NOT NULL,
    consecutive_failures   INTEGER NOT NULL DEFAULT 0,
    last_success_at        TEXT,
    last_failure_at        TEXT,
    last_error             TEXT,
    last_alert_sent_at     TEXT,
    -- The operational run_id that last advanced this streak. Lets
    -- update_collector_health() recognise a retried run_operational_cycle()
    -- invocation (e.g. crashed then re-run under the same run_id) and skip
    -- advancing the streak a second time for what is logically one failed
    -- or successful collection attempt -- see that method's docstring.
    last_processed_run_id  TEXT,
    PRIMARY KEY (bank, product_type)
);

-- Per-channel companion to collector_health.last_alert_sent_at. That
-- bank-level column only answers "was ANY channel notified of this
-- streak's incident" -- enough to gate whether a recovery is worth
-- attempting at all (see orchestration.classify_health_escalation()), but
-- not which channels to actually send the recovery to. When webhook and
-- Telegram are both configured and only one delivers the incident (the
-- other times out or errors), sending the recovery to both anyway would
-- misleadingly tell the channel that missed the incident it has "recovered"
-- from an outage it was never told about -- see
-- orchestration._dispatch_health_alerts()'s per-channel recovery gating.
-- One row per (bank, product_type, channel), replaced (not appended) on
-- each confirmed delivery -- unlike notified_alerts, this only ever needs
-- the *latest* delivery per channel, not a full history.
CREATE TABLE IF NOT EXISTS collector_health_alert_channels (
    bank            TEXT NOT NULL,
    product_type    TEXT NOT NULL,
    channel         TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    PRIMARY KEY (bank, product_type, channel)
);

-- Auto-triggered pricing-scenario comparisons (see
-- pricing_intelligence.scenario_trigger), one row per (run_id, term_months)
-- that a meaningful competitor move triggered analysis for. Keyed on run_id
-- rather than a rate_changes.id foreign key -- run_id already identifies
-- both the triggering operational_runs row and every rate_changes row it
-- produced, and a term can have more than one contributing change (several
-- banks moving the same term in one run) with no single natural change to
-- point at instead.
CREATE TABLE IF NOT EXISTS scenario_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    product_type  TEXT NOT NULL,
    term_months   INTEGER NOT NULL,
    generated_at  TEXT NOT NULL,
    scenarios_json TEXT NOT NULL,
    UNIQUE (run_id, product_type, term_months)
);

CREATE INDEX IF NOT EXISTS idx_scenario_runs_run_id ON scenario_runs (run_id);
""" + _RATE_CHANGES_SCHEMA

# Fields whose movement makes an observation a genuinely new advertised state.
# Provenance fields (hashes, page titles, raw text) are deliberately excluded:
# a bank reformatting its page must not read as a pricing change.
_MATERIAL_FIELDS = (
    "rate_pct",
    "product_name",
    "eligibility_text",
    "lvr_condition",
    "max_lvr",
    "min_equity",
    "new_business_only",
    "cashback_text",
    "promotion_text",
    "package_requirement",
    "min_balance",
    "max_balance",
)

# Term identity matching CompetitorRateRecord.eligibility_group_key(): when
# term_months_normalised is available on *both* sides, that numeric value is
# the whole comparison and the raw label is irrelevant (a bank rewording "1
# year" to "12 months" is still the same term); only falls back to comparing
# the raw label when normalisation is missing on both sides.
_SAME_TERM_IDENTITY = (
    "("
    "  (o3.term_months IS NOT NULL AND o.term_months IS NOT NULL "
    "   AND o3.term_months = o.term_months)"
    "  OR (o3.term_months IS NULL AND o.term_months IS NULL "
    "      AND LOWER(o3.term_label_raw) = LOWER(o.term_label_raw))"
    ")"
)

def _run_ordered_after(
    later_run_column: str,
    later_timestamp_column: str,
    earlier_run_column: str,
    earlier_timestamp_column: str,
) -> str:
    """SQL fragment: true when the run named by ``later_run_column`` comes
    strictly after the run named by ``earlier_run_column``.

    Compares each side's ``collection_runs`` row by ``(started_at, rowid)``
    -- the same ordering :meth:`ObservationStore.latest_run_id` and
    :meth:`ObservationStore._as_of_cutoff` use -- when both correlate to a
    stored run, so two events sharing a timestamp are still ordered
    correctly by which run actually happened first. Falls back to comparing
    ``later_timestamp_column``/``earlier_timestamp_column`` directly when
    either run_id does not correlate to a stored run at all (matching this
    comparison's original, timestamp-only behaviour for that case -- see
    :meth:`ObservationStore._as_of_cutoff`'s docstring for why an
    unresolvable run_id needs this fallback).

    Unlike ``_as_of_cutoff()``'s fragments (which compare one row's
    introducing run against a single fixed reference run, supplied via query
    params), this compares two run_id *columns* against each other directly
    within the same query -- e.g. "did this withdrawal happen in a run
    strictly after the one that introduced this state" or "did this
    eligibility-superseding sibling first appear in a run strictly after the
    one that last confirmed this row" -- so it takes no params of its own;
    both operands are column references already present in the query.
    """
    return (
        "("
        f"EXISTS (SELECT 1 FROM collection_runs cr_later, collection_runs cr_earlier "
        f"WHERE cr_later.run_id = {later_run_column} AND cr_earlier.run_id = {earlier_run_column} "
        "AND (cr_later.started_at > cr_earlier.started_at "
        "OR (cr_later.started_at = cr_earlier.started_at AND cr_later.rowid > cr_earlier.rowid))) "
        "OR ("
        f"(NOT EXISTS (SELECT 1 FROM collection_runs WHERE run_id = {later_run_column}) "
        f"OR NOT EXISTS (SELECT 1 FROM collection_runs WHERE run_id = {earlier_run_column})) "
        f"AND {later_timestamp_column} > {earlier_timestamp_column}"
        ")"
        ")"
    )


# Excludes row `o` when another key `o3` in the same eligibility group (same
# bank/term/category/frequency/product_name/balance tier, i.e. the same
# product under a different eligibility condition -- see
# CompetitorRateRecord.eligibility_group_key()) first appeared after `o` was
# last confirmed: that is the signature of an eligibility-edit transition,
# where change detection deliberately leaves the old key's row untouched
# (see changes.resolve_pairing()) rather than closing it out. Compared by run
# position (see _run_ordered_after()), not a plain first_observed_at/
# last_seen_at timestamp check: two runs sharing a timestamp -- the old key
# last confirmed by one, the new key introduced by the other -- would
# otherwise fail this test in whichever direction ties read as "not after".
# `{extra}` lets a caller reconstructing a historical point in time
# (get_observations_as_of) additionally bound `o3` to have existed by that
# point too -- an eligibility edit that happens after the reconstruction
# point does not apply.
_SUPERSEDED_BY_LATER_ELIGIBILITY_KEY = (
    "AND NOT EXISTS ("
    "  SELECT 1 FROM observations o3 WHERE o3.observation_key <> o.observation_key "
    "  AND o3.bank = o.bank AND o3.product_type = o.product_type "
    "  AND o3.rate_category = o.rate_category AND " + _SAME_TERM_IDENTITY + " "
    "  AND o3.interest_payment_frequency = o.interest_payment_frequency "
    "  AND LOWER(o3.product_name) = LOWER(o.product_name) "
    "  AND o3.min_balance IS o.min_balance AND o3.max_balance IS o.max_balance "
    "  AND "
    + _run_ordered_after(
        "o3.first_seen_run_id", "o3.first_observed_at", "o.last_seen_run_id", "o.last_seen_at"
    )
    + "{extra}"
    ") "
)


class ChronologicalOrderError(ValueError):
    """A collection was written behind the product's latest recorded run.

    Subclasses :class:`ValueError` so callers written against the original
    guard keep working. Named so an unattended worker can tell a backwards
    clock (an NTP correction, a restored VM snapshot) apart from a genuine
    programming error and report it as a classified failure rather than dying
    on an unhandled traceback -- see ``scripts/collect_market_rates_workflow.py``.
    """



@dataclass(frozen=True)
class WriteResult:
    """Outcome of persisting one collection run."""

    run_id: str
    inserted: int
    unchanged: int

    @property
    def total(self) -> int:
        return self.inserted + self.unchanged


@dataclass(frozen=True)
class RunRecord:
    """One operational run of :func:`~.orchestration.run_operational_cycle`.

    Distinct from ``collection_runs`` (written by :meth:`record_observations`,
    keyed by whatever run_id that call is given): this is the orchestration
    layer's own ledger, and it always gets a row -- including a total-failure
    run where ``record_observations`` is never reached at all (zero accepted
    records short-circuits before persistence), which ``collection_runs``
    cannot represent. ``collection_run_id`` links back to that row when one
    exists, for cross-referencing.
    """

    run_id: str
    started_at: datetime
    completed_at: datetime
    status: str  # "SUCCESS" | "PARTIAL_SUCCESS" | "FAILED"
    product_type: ProductType
    banks_attempted: tuple[str, ...]
    banks_successful: tuple[str, ...]
    banks_failed: tuple[str, ...]
    rates_observed: int
    rates_changed: int
    validation_failures: int
    suspicious_observations: int
    market_state: Optional[str]
    error_summary: Optional[str]
    collection_run_id: Optional[str] = None


@dataclass(frozen=True)
class CollectorHealth:
    """Current failure-streak state for one bank/product collector."""

    bank: str
    product_type: ProductType
    consecutive_failures: int
    last_success_at: Optional[datetime]
    last_failure_at: Optional[datetime]
    last_error: Optional[str]
    last_alert_sent_at: Optional[datetime]
    #: The operational run_id that last advanced this streak, or None for a
    #: pre-migration row or one never updated with a run_id. See
    #: ObservationStore.update_collector_health()'s retry-dedup docstring.
    last_processed_run_id: Optional[str] = None


@dataclass(frozen=True)
class IntegrityReport:
    """Result of :meth:`ObservationStore.check_integrity`."""

    ok: bool
    checks: dict[str, bool]
    issues: tuple[str, ...]


class PreviousRun(NamedTuple):
    """The run immediately before a reference run, resolved atomically.

    See :meth:`ObservationStore.get_previous_run` -- a caller needing both
    the run_id and started_at of "the previous run" must resolve them
    together from one lookup, not via two separate calls that could each
    resolve "previous" relative to a different latest run.
    """

    run_id: str
    started_at: datetime


class _AsOfCutoff(NamedTuple):
    """SQL fragments and params built by :meth:`ObservationStore._as_of_cutoff`."""

    outer: str
    inner: str
    eligibility: str
    withdrawal: str
    observation_params: tuple
    withdrawal_params: tuple


def _iso(value: datetime) -> str:
    """Serialise a timestamp as an unambiguous UTC ISO-8601 string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _run_record_from_row(row: sqlite3.Row) -> "RunRecord":
    return RunRecord(
        run_id=row["run_id"],
        started_at=_parse_iso(row["started_at"]),
        completed_at=_parse_iso(row["completed_at"]) if row["completed_at"] else None,
        status=row["status"],
        product_type=ProductType(row["product_type"]),
        banks_attempted=tuple(json.loads(row["banks_attempted"])),
        banks_successful=tuple(json.loads(row["banks_successful"])),
        banks_failed=tuple(json.loads(row["banks_failed"])),
        rates_observed=row["rates_observed"],
        rates_changed=row["rates_changed"],
        validation_failures=row["validation_failures"],
        suspicious_observations=row["suspicious_observations"],
        market_state=row["market_state"],
        error_summary=row["error_summary"],
        collection_run_id=row["collection_run_id"],
    )


def _collector_health_from_row(row: sqlite3.Row) -> "CollectorHealth":
    return CollectorHealth(
        bank=row["bank"],
        product_type=ProductType(row["product_type"]),
        consecutive_failures=row["consecutive_failures"],
        last_success_at=_parse_iso(row["last_success_at"]) if row["last_success_at"] else None,
        last_failure_at=_parse_iso(row["last_failure_at"]) if row["last_failure_at"] else None,
        last_error=row["last_error"],
        last_alert_sent_at=(
            _parse_iso(row["last_alert_sent_at"]) if row["last_alert_sent_at"] else None
        ),
        last_processed_run_id=row["last_processed_run_id"],
    )


class ObservationStore:
    """Repository over historical rate observations and detected changes."""

    def __init__(self, database_path: str | Path = ":memory:", *, read_only: bool = False) -> None:
        self.database_path = str(database_path)
        self._temp_database_path: Optional[str] = None
        connect_path = self.database_path
        if read_only and self.database_path != ":memory:":
            # A dry run must never create or migrate the real database file --
            # work against a throwaway copy instead, so schema/migration
            # writes (and any accidental row writes) land there, not on disk
            # at the configured path. A database that doesn't exist yet has
            # nothing to copy, so an in-memory store (starts empty, same as a
            # fresh file would) stands in for it without touching disk at all.
            if Path(self.database_path).exists():
                fd, temp_path = tempfile.mkstemp(suffix=".sqlite3")
                os.close(fd)
                # A plain file copy would miss committed pages still sitting
                # in a WAL sidecar (never checkpointed into the main file),
                # and could tear mid-write if the database is open
                # elsewhere -- e.g. the other product's independently locked
                # collection run. sqlite3's own backup API reads through a
                # live connection, so it always sees a transactionally
                # consistent snapshot, the same reason backup_database()
                # uses it instead of shutil.copyfile.
                source = sqlite3.connect(self.database_path)
                try:
                    destination = sqlite3.connect(temp_path)
                    try:
                        source.backup(destination)
                    finally:
                        destination.close()
                finally:
                    source.close()
                connect_path = temp_path
                self._temp_database_path = temp_path
            else:
                connect_path = ":memory:"
        if connect_path != ":memory:":
            Path(connect_path).parent.mkdir(parents=True, exist_ok=True)
        # A single shared connection keeps an in-memory store usable across
        # calls, which is what the tests and the CLI both want.
        self._connection = sqlite3.connect(connect_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._transaction_depth = 0
        self._create_schema()

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        self._connection.close()
        if self._temp_database_path is not None:
            Path(self._temp_database_path).unlink(missing_ok=True)
            self._temp_database_path = None

    def __enter__(self) -> "ObservationStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # Reentrant: sqlite3's own connection context manager commits on
        # __exit__ regardless of nesting, so a write method called from
        # inside atomic() would otherwise commit early on its own, defeating
        # the whole point of grouping several writes into one commit unit.
        # Only the outermost call actually opens/commits the transaction;
        # nested calls just yield.
        if self._transaction_depth > 0:
            self._transaction_depth += 1
            try:
                yield self._connection
            finally:
                self._transaction_depth -= 1
            return
        self._transaction_depth += 1
        try:
            with self._connection:
                yield self._connection
        finally:
            self._transaction_depth -= 1

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Group several writes (e.g. :meth:`record_observations`,
        :meth:`record_changes`, :meth:`confirm_suspicious_changes`) into one
        commit unit, so they all persist or all roll back together.

        Each of those methods normally commits independently. Called without
        this, a failure between them (e.g. record_changes() raising after
        record_observations() already committed) would leave the new
        observation state persisted with no corresponding audit event: a
        retry of the same collection then compares against the
        already-updated state, sees no movement, and the missed change is
        never reconstructed.
        """
        with self._transaction():
            yield

    def _create_schema(self) -> None:
        with self._transaction() as connection:
            connection.executescript(_SCHEMA)
            self._migrate_collection_runs_product_type(connection)
            self._migrate_observations_first_seen_run_id(connection)
            self._migrate_rate_changes_run_scoped_uniqueness(connection)
            self._migrate_collection_run_products(connection)
            self._migrate_rate_named_mortgages(connection)
            self._migrate_collector_health_run_id(connection)
            row = connection.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )

    def _migrate_rate_named_mortgages(self, connection: sqlite3.Connection) -> None:
        """Repair old rate-first mortgage identities, preserving history.

        Only a product name consisting entirely of a percentage/unit qualifies.
        Prior new/removed events created by that parser defect remain available
        but are flagged; this migration cannot independently verify their meaning.

        The ``rate_changes`` rewrite is ``UPDATE OR IGNORE`` because several
        rate-named variants of one product collapse onto a single identity:
        events the old parser emitted for two of them in the same run then
        share (observation_key, change_type, detected_at, run_id), which that
        table is unique on. Skipping the collision leaves those rows under
        their old rate-named key -- still readable for audit -- whereas
        raising would abort ``_create_schema`` on *every* open and leave the
        database permanently unopenable.

        ``suspicious`` is set in a separate, unconditional ``UPDATE`` before
        that collision-prone rewrite is attempted, not combined into it: a
        row the rewrite skips would otherwise keep ``suspicious = 0`` under
        its original rate-named key, which
        :meth:`get_changes_since`\\ (``include_suspicious=False``) and market-
        activity calculations then read as a genuine, unflagged event rather
        than the known parser artifact it is. The plain ``UPDATE`` here only
        touches ``suspicious``, never the table's unique columns, so it
        cannot itself collide -- every row under the old key ends up flagged
        regardless of whether its identity rewrite later succeeds.
        """
        rows = connection.execute(
            "SELECT * FROM observations WHERE product_type = 'home_loan' "
            "AND term_months IS NOT NULL"
        ).fetchall()
        candidates = [
            row
            for row in rows
            if re.fullmatch(r"\d+(?:\.\d+)?\s*%\s*(?:p\.?\s*a\.?)?", row["product_name"], re.I)
        ]

        # Grouped by the identity each row repairs to *and* the run that
        # introduced it, not applied one row at a time or grouped by
        # identity alone: the rate is deliberately absent from identity, so
        # a legacy product legitimately repriced across separate runs has
        # every one of its historical rate-named rows repair to the same
        # identity too -- that is an ordinary reprice history, not a
        # collision, and grouping by identity alone would quarantine the
        # latest state right along with it, leaving it no confirmed
        # fallback and dropping it from market snapshots. Only rows sharing
        # both the repaired identity *and* first_seen_run_id are a genuine
        # collision: the source page advertised two conflicting rate-named
        # readings for what should be one product in a single collection
        # attempt. first_seen_run_id is guaranteed backfilled by this point
        # -- _migrate_observations_first_seen_run_id() runs earlier in
        # _create_schema, on every database this migration also runs on.
        repaired_groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in candidates:
            record = _row_to_record(row).model_copy(update={"product_name": row["term_label_raw"]})
            repaired_groups.setdefault(
                (record.observation_key(), row["first_seen_run_id"]), []
            ).append(row)

        skipped = 0
        quarantined = 0
        for group in repaired_groups.values():
            ambiguous = len(group) > 1
            for row in group:
                record = _row_to_record(row).model_copy(
                    update={"product_name": row["term_label_raw"]}
                )
                if ambiguous:
                    connection.execute(
                        "UPDATE observations SET product_name = ?, observation_key = ?, "
                        "validation_status = 'warning' WHERE id = ?",
                        (record.product_name, record.observation_key(), row["id"]),
                    )
                    quarantined += 1
                else:
                    connection.execute(
                        "UPDATE observations SET product_name = ?, observation_key = ? "
                        "WHERE id = ?",
                        (record.product_name, record.observation_key(), row["id"]),
                    )
                connection.execute(
                    "UPDATE rate_changes SET suspicious = 1 WHERE observation_key = ?",
                    (row["observation_key"],),
                )
                expected = connection.execute(
                    "SELECT COUNT(*) FROM rate_changes WHERE observation_key = ?",
                    (row["observation_key"],),
                ).fetchone()[0]
                updated = connection.execute(
                    "UPDATE OR IGNORE rate_changes SET product_name = ?, observation_key = ? "
                    "WHERE observation_key = ?",
                    (record.product_name, record.observation_key(), row["observation_key"]),
                ).rowcount
                skipped += expected - updated
        if quarantined:
            logger.warning(
                "rate-named identity migration found %d observation(s) whose repaired identity "
                "collides with another row from the same run: quarantined as "
                "validation_status='warning' (excluded from get_current_observations() and "
                "market statistics) rather than let get_current_observations() arbitrarily pick "
                "one of the colliding rates as current. Review against the source before "
                "clearing the warning.",
                quarantined,
            )
        if skipped:
            logger.warning(
                "rate-named identity migration left %d change event(s) under their original "
                "key: another event already occupies the repaired identity for the same run. "
                "Review these historical events against the source before relying on them.",
                skipped,
            )

    def _migrate_collector_health_run_id(self, connection: sqlite3.Connection) -> None:
        """Add ``collector_health.last_processed_run_id`` for a database
        created before this column existed.

        No backfill: the concept ("which operational run last advanced this
        streak") did not exist before this column, so there is nothing
        historical to recover -- every pre-existing row simply starts with
        ``NULL``, which :meth:`update_collector_health` already treats as
        "no known prior run" (never matches a provided ``run_id``, so the
        first update after upgrade advances normally).
        """
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(collector_health)")}
        if "last_processed_run_id" in columns:
            return
        connection.execute("ALTER TABLE collector_health ADD COLUMN last_processed_run_id TEXT")

    def _migrate_collection_runs_product_type(self, connection: sqlite3.Connection) -> None:
        """Add ``collection_runs.product_type`` for a database created before
        this column existed.

        ``CREATE TABLE IF NOT EXISTS`` in ``_SCHEMA`` is a no-op against an
        already-created table, so a database file from before this column
        existed needs an explicit migration, not just an updated ``CREATE
        TABLE`` statement.

        The ``ALTER TABLE`` below defaults every existing row to
        ``home_loan``, but that default is only a starting point, not the
        answer: the ``--product`` CLI flag has collected term deposits since
        before this column existed, so a database can already hold genuine
        deposit runs that must not be mislabelled. All three ``UPDATE``s
        below correct that from data the pre-existing schema already
        recorded -- none of it is itself new:

        * ``rate_changes.run_id``/``product_type`` are permanent (one row
          per detected change, never overwritten), so they can recover a
          historical run's product even once ``observations`` has moved
          past it -- as long as that run detected at least one change.
        * ``observations.last_seen_run_id`` is overwritten forward on every
          later re-observation (even an unchanged one), so it can only
          answer for whichever run most recently touched each still-current
          row -- typically each key's latest run, not every historical one.
        * ``observations.first_observed_at``, unlike ``last_seen_run_id``, is
          frozen at insertion and never advanced forward -- it still equals
          the introducing run's own ``started_at`` even once ``last_seen_run_id``
          has moved past that run via later reconfirmations. This is what
          recovers a bootstrapping first-ever collection (which, via
          detect_changes()'s own bootstrap suppression, produced zero
          ``NEW_PRODUCT`` events for the ``rate_changes`` pass to find) once
          its rows have since been reconfirmed by a later run: the second
          pass can no longer find it via ``last_seen_run_id``, but the third
          still can via ``first_observed_at``.

        The third pass matches on ``started_at`` value rather than
        ``run_id`` -- ``observations`` has no column recording which run
        first introduced a row, so this is a one-time, best-effort backfill
        for a database that predates :meth:`_migrate_observations_first_seen_run_id`
        (which adds exactly that column, and is what the *live* query
        methods correlate through going forward -- see its own docstring for
        why the ``started_at``-value approach is not safe to use there).
        A run that shares its ``started_at`` with another run of a different
        product (both migrated in the very same pass) could therefore still
        be mislabelled here; this is an accepted, bounded imprecision for
        one-time historical backfill, not an ongoing correctness bug -- but
        only for a run neither of the first two, higher-confidence passes
        could already resolve. The third pass is restricted to exactly those
        runs (excluded from both ``WHERE run_id IN (...)`` conditions
        above): applying it unconditionally would let its own unordered,
        arbitrary ``LIMIT 1`` pick overwrite an *already-correct* label from
        pass one or two whenever a same-timestamp different-product run
        happens to be the one it picks.

        A truly ancient run with no trace in any of the three -- superseded
        in ``observations`` on both timestamps and with zero recorded
        changes -- has no surviving way to recover its product, and keeps
        the ``home_loan`` default.
        """
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(collection_runs)")}
        if "product_type" in columns:
            return
        connection.execute(
            "ALTER TABLE collection_runs ADD COLUMN product_type TEXT NOT NULL DEFAULT 'home_loan'"
        )
        connection.execute(
            "UPDATE collection_runs SET product_type = ("
            "  SELECT rc.product_type FROM rate_changes rc "
            "  WHERE rc.run_id = collection_runs.run_id LIMIT 1"
            ") WHERE run_id IN (SELECT DISTINCT run_id FROM rate_changes)"
        )
        connection.execute(
            "UPDATE collection_runs SET product_type = ("
            "  SELECT o.product_type FROM observations o "
            "  WHERE o.last_seen_run_id = collection_runs.run_id LIMIT 1"
            ") WHERE run_id IN (SELECT DISTINCT last_seen_run_id FROM observations)"
        )
        connection.execute(
            "UPDATE collection_runs SET product_type = ("
            "  SELECT o.product_type FROM observations o "
            "  WHERE o.first_observed_at = collection_runs.started_at LIMIT 1"
            ") WHERE started_at IN (SELECT DISTINCT first_observed_at FROM observations) "
            "AND run_id NOT IN (SELECT DISTINCT run_id FROM rate_changes) "
            "AND run_id NOT IN (SELECT DISTINCT last_seen_run_id FROM observations)"
        )

    def _migrate_observations_first_seen_run_id(self, connection: sqlite3.Connection) -> None:
        """Add ``observations.first_seen_run_id`` for a database created
        before this column existed.

        ``last_seen_run_id`` is overwritten forward on every re-observation,
        so it cannot answer "which run first introduced this row" once a
        later collection has reconfirmed it -- exactly the gap
        :meth:`latest_run_id`/:meth:`get_previous_run_started_at` need
        closed for a mixed-product or bootstrapping legacy run (see
        :meth:`_migrate_collection_runs_product_type`'s docstring). This
        column is the direct fix: set once at insertion in
        :meth:`record_observations` and never touched again, so those
        methods can correlate ``observations`` to the run that introduced
        them by an actual ``run_id`` rather than by matching
        ``first_observed_at`` against ``collection_runs.started_at`` --
        matching on that shared *timestamp value* is unsafe as a live,
        ongoing correlation: two different runs captured with the same
        ``observed_at`` (e.g. a caller collecting home-loan and
        term-deposit batches from one shared capture moment) would then
        each appear to contain the other's rows too, and the query's own
        ``ORDER BY ... rowid DESC`` tie-break could return the wrong run
        for either product -- a live bug, not just a one-time migration
        imprecision, because it would recur on every simultaneous
        multi-product collection rather than being confined to backfilling
        historical data once.

        Three passes, in descending order of confidence, each only touching
        what the previous one left ``NULL``:

        1. An exact ``rate_changes`` match: a ``NEW_PRODUCT`` event recorded
           for this exact ``observation_key`` at this exact
           ``first_observed_at`` names the introducing ``run_id`` directly,
           no timestamp-ordering guess needed. A bootstrapping first-ever
           collection produces none of these (``detect_changes()``'s own
           bootstrap suppression), so this pass only ever helps a later,
           non-bootstrap introduction -- exactly the gap the two passes below
           cannot resolve unambiguously.

           Restricted to the highest-``id`` row per ``(observation_key,
           first_observed_at)`` group: a legacy product bootstrapped, then
           withdrawn, then returned across a run sharing the bootstrap run's
           own ``started_at`` produces two ``observations`` rows tied on
           that pair (the untouched bootstrap row and the fresh return row),
           but only one ``NEW_PRODUCT`` event -- from the return -- exists at
           that ``detected_at`` (a pre-existing legacy database's rows were
           written while ``rate_changes`` was unique on ``(observation_key,
           change_type, detected_at)`` alone, before :meth:`_migrate_rate_changes_run_scoped_uniqueness`
           widened that to include ``run_id``, so at most one such row could
           ever have been recorded for it). Without this restriction the
           correlated subquery matches *both* tied rows and
           assigns the return's ``run_id`` to the bootstrap row too, so a
           later ``get_observations_as_of(before_run_id=<bootstrap run>)``
           would see the bootstrap row as introduced by the return and
           wrongly omit a product that was genuinely present. The excluded
           (non-latest) row is left ``NULL`` here and resolved unambiguously
           by pass two or three instead, which key off ``collection_runs``
           row order rather than this event match.
        2. The timestamp-and-product match below -- the same caveat as
           :meth:`_migrate_collection_runs_product_type`'s own third pass
           applies, and for the same reason: it is a one-time correction for
           existing rows, not a live query path. It also runs after that
           method, so ``collection_runs.product_type`` is already backfilled
           and narrows the match to the correct product where two runs would
           otherwise collide on ``started_at`` alone. Still ambiguous when
           *two same-product* runs share a timestamp (picks the earliest by
           ``rowid``), which is exactly why pass 1 is tried first wherever it
           can answer precisely.

           That product filter is itself only safe when the legacy run was
           genuinely single-product. A pre-column bootstrap run that
           collected *both* products at once has exactly one
           ``collection_runs`` row, so
           ``_migrate_collection_runs_product_type`` can only have backfilled
           a single ``product_type`` label onto it -- whichever the priority
           passes in its docstring recovered evidence for. That mislabels the
           *other* product's observations from the same run: this pass finds
           no ``collection_runs`` row at all for them and leaves
           ``first_seen_run_id`` ``NULL``, making that run invisible to
           :meth:`get_previous_run_started_at` for that product even though
           it was genuinely present.
        3. An unfiltered timestamp-only match, recovering the mixed-bootstrap
           case pass 2 leaves ``NULL`` by relaxing the product filter.
        """
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(observations)")}
        if "first_seen_run_id" in columns:
            return
        connection.execute("ALTER TABLE observations ADD COLUMN first_seen_run_id TEXT")
        connection.execute(
            "UPDATE observations SET first_seen_run_id = ("
            "  SELECT rc.run_id FROM rate_changes rc "
            "  WHERE rc.observation_key = observations.observation_key "
            "  AND rc.change_type = 'NEW_PRODUCT' "
            "  AND rc.detected_at = observations.first_observed_at "
            "  LIMIT 1"
            ") WHERE EXISTS ("
            "  SELECT 1 FROM rate_changes rc2 "
            "  WHERE rc2.observation_key = observations.observation_key "
            "  AND rc2.change_type = 'NEW_PRODUCT' "
            "  AND rc2.detected_at = observations.first_observed_at"
            ") AND observations.id = ("
            "  SELECT MAX(o2.id) FROM observations o2 "
            "  WHERE o2.observation_key = observations.observation_key "
            "  AND o2.first_observed_at = observations.first_observed_at"
            ")"
        )
        connection.execute(
            "UPDATE observations SET first_seen_run_id = ("
            "  SELECT cr.run_id FROM collection_runs cr "
            "  WHERE cr.started_at = observations.first_observed_at "
            "  AND cr.product_type = observations.product_type "
            "  ORDER BY cr.rowid LIMIT 1"
            ") WHERE first_seen_run_id IS NULL"
        )
        connection.execute(
            "UPDATE observations SET first_seen_run_id = ("
            "  SELECT cr.run_id FROM collection_runs cr "
            "  WHERE cr.started_at = observations.first_observed_at "
            "  ORDER BY cr.rowid LIMIT 1"
            ") WHERE first_seen_run_id IS NULL"
        )

    def _migrate_rate_changes_run_scoped_uniqueness(self, connection: sqlite3.Connection) -> None:
        """Widen ``rate_changes``' uniqueness to include ``run_id``, for a
        database created before this column joined the constraint.

        ``record_changes()`` relies on ``INSERT OR IGNORE`` plus this
        uniqueness for idempotency: retrying the exact same detected changes
        (the same objects, hence the same ``run_id``) must not create
        duplicate rows. But scoped to only ``(observation_key, change_type,
        detected_at)``, that same guard also silently drops a *genuinely new*
        event from a *later* run whenever an earlier run's event happens to
        share the exact same ``detected_at`` -- e.g. four runs sharing one
        ``started_at`` (observed, withdrawn, returned, withdrawn again) each
        try to record their own event for this key, but the second
        ``REMOVED_PRODUCT`` collides with the first's and is discarded.
        ``get_current_observations()`` then finds no ``REMOVED_PRODUCT``
        run-ordered after the row's last touch and keeps serving the
        withdrawn rate. Including ``run_id`` in the constraint keeps
        same-run retries idempotent (they always share one run_id) while
        letting every run's own event persist independently.

        SQLite cannot ``ALTER`` a table-level ``UNIQUE`` constraint in place,
        so this recreates the table under its new definition (identical to
        :data:`_RATE_CHANGES_SCHEMA`, which ``_SCHEMA`` also uses) and copies
        every existing row across -- safe because no two of them could have
        ever violated the *new*, less restrictive constraint if they didn't
        already violate the old one.

        Explicitly drops ``idx_changes_detected_at`` right after the rename,
        before creating the new table: ``ALTER TABLE ... RENAME`` carries an
        index across onto the renamed table under its *original* name, so
        without this, :data:`_RATE_CHANGES_SCHEMA`'s own ``CREATE INDEX IF
        NOT EXISTS idx_changes_detected_at`` finds that name already taken
        (by the old table) and silently skips creating one for the new
        table -- then dropping the old table drops its now-orphaned index
        along with it, leaving the new ``rate_changes`` with no index on
        ``detected_at`` at all for :meth:`get_changes_since`'s window
        queries.
        """
        already_run_scoped = any(
            index["unique"]
            and {
                info["name"]
                for info in connection.execute(f"PRAGMA index_info({index['name']!r})")
            }
            == {"observation_key", "change_type", "detected_at", "run_id"}
            for index in connection.execute("PRAGMA index_list('rate_changes')")
        )
        if already_run_scoped:
            return
        columns = ", ".join(
            row["name"] for row in connection.execute("PRAGMA table_info('rate_changes')")
        )
        connection.execute("ALTER TABLE rate_changes RENAME TO rate_changes_pre_run_scoped_unique")
        connection.execute("DROP INDEX IF EXISTS idx_changes_detected_at")
        connection.executescript(_RATE_CHANGES_SCHEMA)
        connection.execute(
            f"INSERT INTO rate_changes ({columns}) "
            f"SELECT {columns} FROM rate_changes_pre_run_scoped_unique"
        )
        connection.execute("DROP TABLE rate_changes_pre_run_scoped_unique")

    def _migrate_collection_run_products(self, connection: sqlite3.Connection) -> None:
        """Backfill ``collection_run_products`` from whatever evidence of
        historical (run_id, product_type) participation -- and its timing --
        still survives.

        ``CREATE TABLE IF NOT EXISTS`` in ``_SCHEMA`` already creates this
        table (empty) for every database, new or pre-existing. A database
        that already has the table but predates its ``started_at`` column
        (this store's own previous revision) gets that column added here,
        defaulting to ``''`` -- corrected immediately below by the same
        backfill passes a genuinely pre-existing database needs, so no
        row is left with the placeholder.

        Recovers rows (and each one's ``started_at``) from the same three
        signals :meth:`latest_run_id` correlates through:

        1. ``collection_runs.product_type``/``started_at`` themselves,
           together with the run's already-recorded ``inserted_count``/
           ``unchanged_count`` -- the one source that can recover the *real*
           original counts, for whichever product currently owns that
           single-value pair. If a second product later reused this
           ``run_id`` (see :meth:`record_observations`'s own docstring),
           ``started_at`` here already reflects *that* later call, not this
           product's own original timing -- an accepted, one-time-backfill
           imprecision this migration cannot undo, the same way
           :meth:`_migrate_collection_runs_product_type`'s docstring accepts
           an equivalent one for its own third pass.
        2. ``observations.last_seen_run_id``/``first_seen_run_id`` paired
           with ``product_type``, and ``rate_changes.run_id`` paired with
           ``product_type`` -- recovering the *other* product of a legacy
           mixed run (or any run source 1 missed), but with no way to
           recover its real counts, so ``0, 0`` is recorded for these.
           Joined back to ``collection_runs`` for ``started_at``, since
           neither ``observations`` nor ``rate_changes`` records a run's own
           start time.

        All four sources are combined and inserted in a *single* statement,
        ordered by ``collection_runs.rowid`` ascending -- not as four
        separate ``INSERT`` passes, each scanning its own source table in
        whatever order it naturally returns rows. :meth:`latest_run_id`/
        :meth:`_previous_run_row` break a ``started_at`` tie using
        ``collection_run_products.rowid`` (see their own docstrings for why
        that -- not ``collection_runs.rowid`` -- is the one ordering source
        that stays correct for a run_id genuinely reused across products via
        *live* calls, from the database's transitional era between this
        table's introduction and :meth:`record_observations` refusing that
        outright). A backfilled row's own ``collection_run_products.rowid``
        is otherwise just insertion order into *this* table, with no
        relationship to real history on its own -- four separate passes
        would append pass 2/3/4's rows after every one of pass 1's
        regardless of which ``collection_runs.rowid`` each belongs to,
        inverting a mixed run's second-product participation against a
        genuinely later, single-product run sharing its ``started_at``.
        Inserting in one ``rowid``-ordered sweep instead makes every
        backfilled row's freshly-assigned ``collection_run_products.rowid``
        agree, in relative order, with its source ``collection_runs.rowid``
        -- which is all :meth:`latest_run_id` needs from it, without having
        to distinguish "live" from "backfilled" rows at read time.

        When more than one source names the same ``(run_id, product_type)``
        pair (e.g. a normal, non-reused run appears via both source 1 and
        source 2), only source 1's row -- carrying the real counts -- is
        kept; a plain ``UNION``/``INSERT OR IGNORE`` cannot express that
        priority (whichever row happens to reach the primary key first
        would win), so a window function ranks candidates per pair first.

        Run unconditionally on every call rather than gated on "have I
        already migrated": ``INSERT OR IGNORE`` against this table's
        ``(run_id, product_type)`` primary key makes re-running it a no-op
        wherever a row already exists (from a prior migration run, or from
        normal write-time recording by :meth:`record_observations`), so
        there is nothing to gain from a separate has-this-run-before check
        and one fewer thing to keep in sync with the table's own definition.

        This is still only a best-effort historical recovery: a *quiet*
        legacy mixed run (one side merely reconfirmed pre-existing rows,
        producing no ``rate_changes``) whose ``observations`` evidence has
        *already* moved on to a later run by the time this migration runs
        cannot be recovered by any signal here -- there is nothing left to
        read it from. Every write going forward is recorded directly by
        :meth:`record_observations`, with its own accurate ``started_at``
        and correctly-ordered ``rowid``, so this gap cannot reopen once a
        database is running under this schema.
        """
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info('collection_run_products')")
        }
        if "started_at" not in columns:
            connection.execute(
                "ALTER TABLE collection_run_products ADD COLUMN started_at TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            "WITH candidates AS ("
            "  SELECT cr.run_id AS run_id, cr.product_type AS product_type, "
            "         cr.started_at AS started_at, cr.inserted_count AS inserted_count, "
            "         cr.unchanged_count AS unchanged_count, cr.rowid AS source_rowid, 0 AS priority "
            "  FROM collection_runs cr "
            "  UNION ALL "
            "  SELECT o.last_seen_run_id, o.product_type, cr.started_at, 0, 0, cr.rowid, 1 "
            "  FROM observations o JOIN collection_runs cr ON cr.run_id = o.last_seen_run_id "
            "  WHERE o.last_seen_run_id IS NOT NULL "
            "  UNION ALL "
            "  SELECT o.first_seen_run_id, o.product_type, cr.started_at, 0, 0, cr.rowid, 1 "
            "  FROM observations o JOIN collection_runs cr ON cr.run_id = o.first_seen_run_id "
            "  WHERE o.first_seen_run_id IS NOT NULL "
            "  UNION ALL "
            "  SELECT rc.run_id, rc.product_type, cr.started_at, 0, 0, cr.rowid, 1 "
            "  FROM rate_changes rc JOIN collection_runs cr ON cr.run_id = rc.run_id"
            "), ranked AS ("
            "  SELECT *, ROW_NUMBER() OVER ("
            "           PARTITION BY run_id, product_type ORDER BY priority ASC"
            "         ) AS rn "
            "  FROM candidates"
            ") "
            "INSERT OR IGNORE INTO collection_run_products "
            "(run_id, product_type, started_at, inserted_count, unchanged_count) "
            "SELECT run_id, product_type, started_at, inserted_count, unchanged_count "
            "FROM ranked WHERE rn = 1 ORDER BY source_rowid ASC"
        )
        # A row created by the ADD COLUMN default above, for a run_id/product
        # none of the sources above touched (its only join target,
        # collection_runs, no longer has a matching row at all) -- keeps
        # this migration from leaving a placeholder value behind rather than
        # a real one it simply could not resolve. Mirrors the third pass of
        # :meth:`_migrate_collection_runs_product_type`'s own accepted
        # imprecision: no better timestamp survives for it.
        connection.execute(
            "UPDATE collection_run_products SET started_at = ("
            "  SELECT cr.started_at FROM collection_runs cr WHERE cr.run_id = collection_run_products.run_id"
            ") WHERE started_at = '' AND run_id IN (SELECT run_id FROM collection_runs)"
        )

    # ----------------------------------------------------------------- writes

    def record_observations(
        self,
        records: Iterable[CompetitorRateRecord],
        run_id: Optional[str] = None,
        observed_at: Optional[datetime] = None,
        report_json: Optional[str] = None,
        product_type: Optional[ProductType | str] = None,
    ) -> WriteResult:
        """Persist one collection run, collapsing unchanged re-observations.

        Returns counts of newly-inserted advertised states versus unchanged
        re-observations, so a caller can tell a quiet day from a repricing day
        without querying further.

        ``collection_runs.product_type`` is taken from the records themselves
        when ``product_type`` is not supplied, defaulting to ``HOME_LOAN`` for
        an empty ``records`` so a run is still recorded consistently with this
        store's pre-term-deposit history. This is what lets
        :meth:`latest_run_id`/:meth:`get_previous_run_started_at` resolve "the
        latest run" *for a given product* now that home-loan and term-deposit
        collections share one ``collection_runs`` table -- which only works if
        every record in one call belongs to the same product: a caller seeding
        a store directly must do the same, one call per product, rather than
        passing a mixed batch that only one product's runs could ever be found
        under.

        Inferring solely from ``records`` silently mislabels a call with zero
        records (e.g. a term-deposit collection where every bank withdrew, or
        a failed attempt worth recording as an empty run) as ``HOME_LOAN``
        regardless of which product was actually being collected -- it can
        then surface as the latest home-loan run for previous-snapshot and
        run-scoped history queries, while that product's own run history loses
        all trace the attempt happened. A caller that already knows the
        product -- :meth:`~.service.PricingIntelligenceService.collect` always
        does -- should pass ``product_type`` explicitly instead of relying on
        inference. When both are given, ``product_type`` must agree with every
        record's own ``product_type``, catching a caller's mismatch rather
        than silently trusting the explicit value over genuinely conflicting
        records.

        Idempotent when ``run_id`` names a run this store has already
        recorded *for this same product*: returns that run's
        originally-recorded counts immediately, without touching
        ``observations`` at all. Preserving ``collection_runs``' row
        position on a retried ``run_id`` (see the upsert below) is not
        sufficient on its own -- replaying the same records through
        :meth:`_upsert_observation` would still move ``last_seen_run_id`` on
        every unchanged one back to the retried (older) run, even though a
        genuinely later run has since touched it. :meth:`get_latest_rates`
        filters on an exact ``last_seen_run_id`` match against
        :meth:`latest_run_id`, so that alone would empty its result for a key
        whose true latest state is correct but whose ``last_seen_run_id`` was
        just dragged backwards. A changed retry payload is worse: it would
        insert a fresh row stamped with the retried run's (older) timestamp,
        which :meth:`get_current_observations` -- keyed on the highest
        ``id`` per ``observation_key`` -- would then read as the newest
        state, silently reviving stale content over whatever the later run
        genuinely recorded. Short-circuiting keeps the *first* recording of
        any given ``run_id`` (for a given product) authoritative, and any
        retry an inert no-op.

        Determined via ``collection_run_products`` -- a durable, write-once
        ledger of every ``(run_id, product_type)`` pair this method has
        processed -- not ``collection_runs.product_type`` (that single
        column can only hold one value, so a legacy run_id that recorded
        both products, one call per product, has it pinned to whichever call
        happened last) nor the ``observations``/``rate_changes`` evidence an
        earlier version of this check fell back to (a later run can move
        ``last_seen_run_id``/``first_seen_run_id`` on, and a quiet
        reconfirmation-only call leaves no ``rate_changes`` row at all --
        either erases the only trace a given run_id ever touched a product).
        See :meth:`_migrate_collection_run_products` for how a pre-existing
        database recovers what it still can of that history.

        Raises ``ValueError`` if ``run_id`` already names a run recorded for
        a *different* product -- reusing one run_id across two calls for
        different products is refused outright now, not just when their
        ``observed_at`` values differ. Even matching timestamps aren't safe:
        ``collection_runs`` holds one ``rowid`` per ``run_id``, preserved
        across the upsert below at whichever position the *first* call
        established it. A third, unrelated run sharing that same timestamp
        and committing between the two calls for this run_id then sits, by
        real insertion order, *between* the run_id's two products' calls --
        but every ordering query that reads ``collection_runs.started_at``/
        ``rowid`` directly (:meth:`_run_ordered_after`,
        :meth:`_run_ordered_after_fixed_reference`, :meth:`_as_of_cutoff`,
        :meth:`get_run_started_at`) still sees only the run_id's one fixed
        position, misordering it against that third run for whichever
        product's call happened second. :meth:`latest_run_id`/
        :meth:`_previous_run_row` resolve their own ordering through
        ``collection_run_products`` instead and would not be fooled, but
        making every ordering path do the same is more places to keep in
        sync than simply not allowing the ambiguity to exist. A ``run_id``
        is meant to name one physical collection event with one position in
        run order; each product's own collection -- even one genuinely
        gathered at the same real-world moment -- gets its own ``run_id``.
        A pre-existing (legacy) database can still hold a genuinely mixed
        run_id from before this guard existed -- see
        :meth:`_migrate_collection_run_products` for how its read-side
        participation is recovered, and ``collection_run_products``'s own
        module-level comment for why that recovery is necessary.
        """
        records = list(records)
        observed_at = observed_at or datetime.now(timezone.utc)
        run_id = run_id or str(uuid.uuid4())
        timestamp = _iso(observed_at)
        record_product_types = {ProductType(record.product_type) for record in records}
        if len(record_product_types) > 1:
            raise ValueError(
                "record_observations() received a mixed-product batch "
                f"({sorted(p.value for p in record_product_types)}); one run must be a single "
                "product's collection -- call it once per product instead."
            )
        if product_type is not None:
            product_type = ProductType(product_type)
            if record_product_types and product_type not in record_product_types:
                raise ValueError(
                    f"record_observations() received product_type={product_type.value!r} but "
                    f"every record's own product_type is "
                    f"{sorted(p.value for p in record_product_types)}"
                )
        else:
            product_type = next(iter(record_product_types), ProductType.HOME_LOAN)

        existing_participation = self._connection.execute(
            "SELECT inserted_count, unchanged_count FROM collection_run_products "
            "WHERE run_id = ? AND product_type = ?",
            (run_id, product_type.value),
        ).fetchone()
        if existing_participation is not None:
            return WriteResult(
                run_id=run_id,
                inserted=existing_participation["inserted_count"],
                unchanged=existing_participation["unchanged_count"],
            )

        existing_run = self._connection.execute(
            "SELECT started_at FROM collection_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if existing_run is not None:
            raise ValueError(
                f"record_observations() received run_id={run_id!r} for product="
                f"{product_type.value!r}, but that run_id is already recorded "
                f"(started_at={existing_run['started_at']!r}) for a different product -- "
                "the same-product case is handled by the idempotent short-circuit above. "
                "collection_runs holds one row (and one rowid) per run_id, which cannot "
                "represent two products' relative ordering against other runs at once, so "
                "reusing one run_id across separate calls for different products is not "
                "supported; use a distinct run_id for each product's call."
            )

        inserted = 0
        unchanged = 0
        banks = sorted({record.bank for record in records})

        with self._transaction() as connection:
            latest = connection.execute(
                "SELECT MAX(started_at) FROM collection_run_products WHERE product_type = ?",
                (product_type.value,),
            ).fetchone()[0]
            if latest is not None and timestamp < latest:
                raise ChronologicalOrderError(
                    "collections must be written in chronological order per product "
                    f"(this run {timestamp}, latest stored {latest})"
                )
            # An upsert (UPDATE on conflict), not INSERT OR REPLACE: the
            # latter deletes and reinserts on a run_id collision, handing the
            # row a brand new (higher) rowid. _run_ordered_after()/
            # _as_of_cutoff() break a started_at tie between two runs by
            # comparing collection_runs.rowid as an insertion-order proxy --
            # retrying an earlier run under its own run_id after a later,
            # same-timestamp run has already committed would otherwise give
            # the retried (semantically earlier) run the higher rowid,
            # reordering it after the later run: latest_run_id() could then
            # select the retried historical run, and run-anchored
            # reconstructions would apply states/withdrawals out of order.
            # UPDATE leaves the original row (and its original rowid) in
            # place.
            connection.execute(
                "INSERT INTO collection_runs "
                "(run_id, started_at, banks, product_type, record_count, inserted_count, "
                "unchanged_count, report_json) VALUES (?, ?, ?, ?, ?, 0, 0, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "started_at = excluded.started_at, banks = excluded.banks, "
                "product_type = excluded.product_type, record_count = excluded.record_count, "
                "inserted_count = 0, unchanged_count = 0, report_json = excluded.report_json",
                (
                    run_id,
                    timestamp,
                    json.dumps(banks),
                    product_type.value,
                    len(records),
                    report_json,
                ),
            )
            for record in records:
                if self._upsert_observation(connection, record, timestamp, run_id):
                    inserted += 1
                else:
                    unchanged += 1
            connection.execute(
                "UPDATE collection_runs SET inserted_count = ?, unchanged_count = ? "
                "WHERE run_id = ?",
                (inserted, unchanged, run_id),
            )
            # Written once, here, and never updated again -- this row is the
            # durable record that this exact (run_id, product_type) has been
            # processed, checked by existing_participation above regardless
            # of what any later run subsequently does to observations/
            # rate_changes.
            connection.execute(
                "INSERT INTO collection_run_products "
                "(run_id, product_type, started_at, inserted_count, unchanged_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, product_type.value, timestamp, inserted, unchanged),
            )

        logger.info(
            "recorded run %s: %s records (%s new states, %s unchanged)",
            run_id,
            len(records),
            inserted,
            unchanged,
        )
        return WriteResult(run_id=run_id, inserted=inserted, unchanged=unchanged)

    def has_run_participation(self, run_id: str, product_type: ProductType | str) -> bool:
        """True when ``run_id`` already has recorded participation for
        ``product_type`` -- i.e. :meth:`record_observations` would
        short-circuit as a no-op (not touch ``observations`` at all) if
        called again with this exact ``(run_id, product_type)`` pair.

        Exposed so a caller like ``service.PricingIntelligenceService.collect()``
        can check this *before* deciding whether to run change detection
        against a freshly-fetched payload: comparing that payload to the
        stored baseline is only meaningful when this call's own write will
        actually land. See ``collect()``'s own handling for why.
        """
        return (
            self._connection.execute(
                "SELECT 1 FROM collection_run_products WHERE run_id = ? AND product_type = ?",
                (run_id, ProductType(product_type).value),
            ).fetchone()
            is not None
        )

    def _run_ordered_after_fixed_reference(
        self,
        connection: sqlite3.Connection,
        reference_run_id: str,
        reference_timestamp: str,
        candidate_run_column: str,
        candidate_timestamp_column: str,
    ) -> tuple:
        """SQL fragment + params: true when the run named by
        ``candidate_run_column`` (a column in the caller's own query) comes
        strictly after one fixed, already-known reference run.

        Complements the module-level :func:`_run_ordered_after`, which
        compares two run_id *columns* within the same query -- here the
        "earlier" side is a single row (e.g. the observation currently being
        upserted) resolved once, up front, rather than a second column.
        Shared by both write-time checks that need exactly this comparison:
        "did this REMOVED_PRODUCT event happen in a run after the one that
        last confirmed this row" (:meth:`_returned_after_a_withdrawal`) and
        "did this eligibility-superseding sibling first appear in a run
        after the one that last confirmed this row" (the eligibility-return
        check in :meth:`_upsert_observation`). Two runs sharing a timestamp
        would otherwise tie under a plain ``timestamp > timestamp`` check,
        merging a same-timestamp return into the stale pre-transition row
        instead of starting a fresh state. Falls back to plain timestamp
        comparison when ``reference_run_id`` itself does not correlate to a
        stored ``collection_runs`` row.
        """
        reference = connection.execute(
            "SELECT started_at, rowid FROM collection_runs WHERE run_id = ?",
            (reference_run_id,),
        ).fetchone()
        if reference is None:
            return f"{candidate_timestamp_column} > ?", (reference_timestamp,)
        ordering = (
            f"(EXISTS (SELECT 1 FROM collection_runs cr WHERE cr.run_id = {candidate_run_column} "
            "AND (cr.started_at > ? OR (cr.started_at = ? AND cr.rowid > ?))) "
            f"OR (NOT EXISTS (SELECT 1 FROM collection_runs WHERE run_id = {candidate_run_column}) "
            f"AND {candidate_timestamp_column} > ?))"
        )
        params = (
            reference["started_at"],
            reference["started_at"],
            reference["rowid"],
            reference_timestamp,
        )
        return ordering, params

    def _returned_after_a_withdrawal(
        self, connection: sqlite3.Connection, observation_key: str, current: sqlite3.Row
    ) -> bool:
        """True when a ``REMOVED_PRODUCT`` event for ``observation_key`` was
        recorded by a run strictly after the one that last touched ``current``.

        Compared by run position (see
        :meth:`_run_ordered_after_fixed_reference`), not a plain
        ``detected_at > last_seen_at`` timestamp check: two runs sharing a
        timestamp -- the row last confirmed by one, withdrawn and then
        reappearing across a later one -- would otherwise tie, merging the
        return into the stale pre-withdrawal row instead of starting a
        fresh state. That corrupts the row's ``first_seen_run_id`` for every
        later run-anchored reconstruction: ``get_market_snapshot(as_of_run_id=
        <return run>)`` would keep seeing the withdrawal as coming after the
        row's (unchanged, pre-withdrawal) introducing run and omit the
        product even though it has genuinely returned.
        """
        ordering, params = self._run_ordered_after_fixed_reference(
            connection, current["last_seen_run_id"], current["last_seen_at"], "rc.run_id", "rc.detected_at"
        )
        return (
            connection.execute(
                "SELECT 1 FROM rate_changes rc WHERE rc.observation_key = ? "
                f"AND rc.change_type = 'REMOVED_PRODUCT' AND {ordering} LIMIT 1",
                (observation_key, *params),
            ).fetchone()
            is not None
        )

    def _returned_after_an_eligibility_transition(
        self, connection: sqlite3.Connection, payload: dict, current: sqlite3.Row
    ) -> bool:
        """True when a sibling key in this row's eligibility group first
        appeared in a run strictly after the one that last touched ``current``.

        Compared by run position (see
        :meth:`_run_ordered_after_fixed_reference`), not a plain
        ``first_observed_at > last_seen_at`` timestamp check: an eligibility
        condition flipping A -> B -> A across three runs sharing one
        timestamp would otherwise tie, since B's ``first_observed_at``
        equals A's ``last_seen_at`` rather than exceeding it. That would
        merge the returning A into its stale pre-transition row instead of
        starting a fresh state, so a later run-anchored reconstruction of
        the middle run would no longer see B as superseding A and both
        eligibility states could appear in the same historical snapshot.
        """
        ordering, params = self._run_ordered_after_fixed_reference(
            connection,
            current["last_seen_run_id"],
            current["last_seen_at"],
            "first_seen_run_id",
            "first_observed_at",
        )
        return (
            connection.execute(
                "SELECT 1 FROM observations WHERE observation_key <> ? "
                "AND bank = ? AND product_type = ? AND rate_category = ? "
                "AND ("
                "  (term_months IS NOT NULL AND ? IS NOT NULL AND term_months = ?)"
                "  OR (term_months IS NULL AND ? IS NULL AND LOWER(term_label_raw) = LOWER(?))"
                ") "
                "AND interest_payment_frequency = ? AND LOWER(product_name) = LOWER(?) "
                f"AND min_balance IS ? AND max_balance IS ? AND {ordering} LIMIT 1",
                (
                    payload["observation_key"],
                    payload["bank"],
                    payload["product_type"],
                    payload["rate_category"],
                    payload["term_months"],
                    payload["term_months"],
                    payload["term_months"],
                    payload["term_label_raw"],
                    payload["interest_payment_frequency"],
                    payload["product_name"],
                    payload["min_balance"],
                    payload["max_balance"],
                    *params,
                ),
            ).fetchone()
            is not None
        )

    def _upsert_observation(
        self,
        connection: sqlite3.Connection,
        record: CompetitorRateRecord,
        timestamp: str,
        run_id: str,
    ) -> bool:
        """Insert a new advertised state, or touch the current one. True if inserted."""
        payload = _record_to_row(record, timestamp, run_id)
        current = connection.execute(
            "SELECT * FROM observations WHERE observation_key = ? "
            "ORDER BY first_observed_at DESC, id DESC LIMIT 1",
            (payload["observation_key"],),
        ).fetchone()

        # A key withdrawn (a REMOVED_PRODUCT event recorded) since we last saw
        # this row is reappearing now, not continuing uninterrupted: force a
        # new state even if the content is identical, so first_observed_at
        # marks the return rather than get_observations_as_of() reading this
        # gap as unbroken presence.
        returned_after_a_withdrawal = current is not None and self._returned_after_a_withdrawal(
            connection, payload["observation_key"], current
        )

        # A product that reverts to an earlier eligibility condition (e.g.
        # 80% LVR -> 75% -> back to 80%) is not a withdrawal -- an
        # ELIGIBILITY_CHANGE never records a REMOVED_PRODUCT for the key it
        # moved away from (that's the whole point of resolve_pairing()'s
        # fallback match). Detected instead via the same eligibility-group
        # sibling signal the read-side supersession check uses: if another
        # key in this product's group started after this row was last
        # confirmed, the identity moved away and back, so merging into this
        # stale row would let its first_observed_at retroactively cover the
        # gap, corrupting both the live view and any as-of reconstruction of
        # the intervening period.
        # Term identity matches CompetitorRateRecord.eligibility_group_key():
        # when term_months is available on both sides that numeric value is
        # the whole comparison (a bank rewording "1 year" to "12 months" is
        # still the same term), regardless of the raw label -- a plain
        # term_label_raw equality check here would miss a transition that
        # eligibility_group_key() already pairs, leaving both rows current.
        returned_after_an_eligibility_transition = (
            current is not None
            and self._returned_after_an_eligibility_transition(connection, payload, current)
        )

        # A repeated suspicious value that is no longer independently flagged
        # this run (0 bps movement against its own warning-tagged baseline)
        # is a confirmation, not a continuation: touching validation_status
        # on the existing row in place would let today's confirmation
        # retroactively rewrite what get_market_snapshot(as_of_run_id=...)
        # or get_previous_market_snapshot() reconstruct for the run when it
        # was still unverified. A fresh row leaves that historical row's
        # "warning" status frozen, exactly like the checks above.
        confirming_a_suspicious_state = (
            current is not None
            and current["validation_status"] == "warning"
            and payload["validation_status"] != "warning"
        )

        if (
            current is not None
            and not returned_after_a_withdrawal
            and not returned_after_an_eligibility_transition
            and not confirming_a_suspicious_state
            and _is_unchanged(current, payload)
        ):
            # A materially unchanged reading still touches its provenance:
            # without refreshing source_url/source_page_title/source_text/
            # content_hash/parser_version too, this row would keep echoing
            # whichever page snapshot happened to be current on its *first*
            # observation, even as last_seen_at advances run after run --
            # an audit of "what does the current reading's evidence look
            # like" would be reading stale HTML/context, not what the most
            # recent collection actually saw.
            connection.execute(
                "UPDATE observations SET last_seen_at = ?, last_seen_run_id = ?, "
                "observation_count = observation_count + 1, "
                "source_url = ?, source_page_title = ?, source_text = ?, "
                "content_hash = ?, parser_version = ? "
                "WHERE id = ?",
                (
                    timestamp,
                    run_id,
                    payload["source_url"],
                    payload["source_page_title"],
                    payload["source_text"],
                    payload["content_hash"],
                    payload["parser_version"],
                    current["id"],
                ),
            )
            return False

        columns = ", ".join(payload)
        placeholders = ", ".join(["?"] * len(payload))
        connection.execute(
            f"INSERT INTO observations ({columns}) VALUES ({placeholders})",
            tuple(payload.values()),
        )
        return True

    def record_changes(self, changes: Sequence["object"]) -> int:
        """Persist detected change events, ignoring ones already stored.

        Accepts ``RateChange`` objects (typed in ``changes.py``; kept structural
        here to avoid a circular import).

        Relies on :data:`_RATE_CHANGES_SCHEMA`'s ``UNIQUE (observation_key,
        change_type, detected_at, run_id)`` for idempotency: retrying this
        call with the exact same ``changes`` (the same objects, hence the
        same ``run_id`` on each) inserts nothing new. Scoped to ``run_id`` so
        a genuinely different run's event is never mistaken for that same
        retry just because it happens to share the same ``detected_at`` as an
        earlier run's event for this key.

        Requires every change to carry a real (truthy) ``run_id`` -- coercing
        a missing one to a shared placeholder (e.g. ``""``) would make that
        run-scoping meaningless: two genuinely different events that both
        omitted ``run_id`` (``RateChange.run_id`` is optional at construction,
        for callers that only need a change for display/alerting and never
        persist it) would collide on the placeholder exactly as they used to
        collide on a shared ``detected_at`` alone, silently dropping the
        later one again. A caller that wants to persist a change must supply
        its own ``run_id`` -- :meth:`~.service.PricingIntelligenceService.collect`
        always does, via :meth:`record_observations`'s freshly generated one.
        """
        written = 0
        with self._transaction() as connection:
            for change in changes:
                if not change.run_id:
                    raise ValueError(
                        "record_changes() requires every change to carry a real run_id "
                        f"(got {change.run_id!r} for observation_key={change.observation_key!r}, "
                        f"change_type={ChangeType(change.change_type).value!r}); rate_changes' "
                        "uniqueness is scoped to run_id specifically so two distinct events "
                        "sharing the same observation_key/change_type/detected_at (e.g. two "
                        "same-timestamp withdrawals) don't collide -- a shared placeholder for "
                        "every omitted run_id would defeat that."
                    )
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO rate_changes ("
                    "observation_key, bank, product_type, product_name, rate_category, "
                    "term_months, change_type, previous_rate_pct, current_rate_pct, change_bps, "
                    "previous_observed_at, detected_at, run_id, suspicious, detail, source_url"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        change.observation_key,
                        change.bank,
                        ProductType(change.product_type).value,
                        change.product_name,
                        change.rate_category,
                        change.term_months,
                        ChangeType(change.change_type).value,
                        change.previous_rate_pct,
                        change.current_rate_pct,
                        change.change_bps,
                        _iso(change.previous_observed_at) if change.previous_observed_at else None,
                        _iso(change.detected_at),
                        change.run_id,
                        int(change.suspicious),
                        change.detail,
                        change.source_url,
                    ),
                )
                written += cursor.rowcount or 0
        return written

    def confirm_suspicious_changes(
        self,
        confirmations: Mapping[
            str, tuple[Optional[CompetitorRateRecord], CompetitorRateRecord]
        ],
    ) -> None:
        """Clear the suspicious flag on the most recent change event for each
        key (and every companion event from the same run), rebasing the rate
        event onto the last verified predecessor where one is known.

        A suspicious-but-accepted movement stays permanently excluded from
        market activity / market-state (both default to skipping suspicious
        changes) even after the same value repeats on a later, clean
        collection -- strong evidence it was a genuine reprice, not a parser
        fault. Called once ``_upsert_observation``'s own confirmation check
        (a repeated value no longer flagged this run) identifies the key.

        An over-threshold rate change that also modifies eligibility or
        promotion data produces companion ``ELIGIBILITY_CHANGE``/
        ``PROMOTION_CHANGE`` events sharing this ``observation_key`` and
        ``detected_at`` with the rate event, all three suspicious together.
        Confirming only the rate event -- the most recent row picked by a
        plain "latest suspicious" lookup, since ``detect_changes()`` sorts
        rate events after their companions -- would leave the companions
        suspicious forever even though the service has already accepted the
        transition. Every suspicious sibling from that same run is cleared
        here too, when it is still accurate to do so (see below).

        A suspicious chain can partially revert (confirmed 5.00% -> warning
        8.00% -> warning 7.00% -> clean 7.00%): the latest stored rate event
        (8.00% -> 7.00%) is an artefact of the intermediate misread, not the
        real movement. When the caller supplies the last verified
        predecessor for a key, the rate row's ``previous_rate_pct``/
        ``change_bps``/``previous_observed_at``/``change_type`` are
        rewritten to reflect the movement from *that* rate (5.00% -> 7.00%,
        +200 bps) rather than merely un-suspicious-ing the stale
        intermediate hop. ``None`` (no known prior confirmed state for this
        key) just clears the rate row's flag, as before.

        A companion sibling is only cleared alongside an *unrebased* rate
        event (its stored ``previous_rate_pct`` already matches the last
        verified predecessor -- the simple, single-hop case). When the rate
        event *is* rebased (a multi-hop chain), a companion sharing that run
        still describes the stale intermediate diff (e.g. "max_lvr:
        0.75 -> 0.70" from the unverified B -> C hop, not the accepted
        A -> C transition) and is left quarantined rather than being
        misleadingly presented as confirmed.

        The confirmed event's *current* endpoint is always this run's actual
        accepted rate (``confirmations``' second element), never the stored
        suspicious row's own ``current_rate_pct``: a parser correction
        between the warning run and this one (confirmed 5.00 -> warning
        6.00 -> clean 5.50, the 6.00 -> 5.50 move itself not suspicious
        since it is within threshold of the warning baseline) never repeats
        6.00 exactly, so using the stale stored value would confirm a
        5.00 -> 6.00 movement that was never actually the final state. That
        intermediate 6.00 -> 5.50 hop already has its own ordinary
        (non-suspicious) rate-change event recorded this run, before this
        method ever runs -- once the confirmed event is rebased to cover
        the true 5.00 -> 5.50 movement, that event describes a hop the
        market never took as a standalone move and is deleted so the same
        reprice is not double-counted as two events.
        """
        rate_types = (ChangeType.RATE_INCREASE.value, ChangeType.RATE_DECREASE.value)
        with self._transaction() as connection:
            for key, (last_verified, current) in confirmations.items():
                row = connection.execute(
                    "SELECT id, current_rate_pct, previous_rate_pct, detected_at, change_type "
                    "FROM rate_changes WHERE observation_key = ? AND suspicious = 1 ORDER BY "
                    "CASE WHEN change_type IN (?, ?) THEN 0 ELSE 1 END, "
                    "detected_at DESC, id DESC LIMIT 1",
                    (key, *rate_types),
                ).fetchone()
                if row is None:
                    continue
                is_rate_row = row["change_type"] in rate_types
                rebasing = (
                    is_rate_row
                    and last_verified is not None
                    and row["previous_rate_pct"] != last_verified.advertised_rate_pct
                )
                if rebasing:
                    connection.execute(
                        "UPDATE rate_changes SET suspicious = 0 WHERE id = ?", (row["id"],)
                    )
                else:
                    connection.execute(
                        "UPDATE rate_changes SET suspicious = 0 WHERE observation_key = ? "
                        "AND suspicious = 1 AND detected_at = ?",
                        (key, row["detected_at"]),
                    )
                if last_verified is None or not is_rate_row:
                    continue
                stale_current_rate_pct = row["current_rate_pct"]
                change_bps = rate_change_bps(
                    last_verified.advertised_rate_pct, current.advertised_rate_pct
                )
                change_type = (
                    ChangeType.RATE_INCREASE if change_bps > 0 else ChangeType.RATE_DECREASE
                )
                connection.execute(
                    "UPDATE rate_changes SET previous_rate_pct = ?, current_rate_pct = ?, "
                    "change_bps = ?, previous_observed_at = ?, change_type = ? WHERE id = ?",
                    (
                        last_verified.advertised_rate_pct,
                        current.advertised_rate_pct,
                        change_bps,
                        _iso(last_verified.captured_at_utc),
                        change_type.value,
                        row["id"],
                    ),
                )
                if stale_current_rate_pct != current.advertised_rate_pct:
                    connection.execute(
                        "DELETE FROM rate_changes WHERE observation_key = ? AND suspicious = 0 "
                        "AND change_type IN (?, ?) AND previous_rate_pct = ? "
                        "AND current_rate_pct = ?",
                        (key, *rate_types, stale_current_rate_pct, current.advertised_rate_pct),
                    )

    def was_notified(self, change: RateChange, channel: str) -> bool:
        """True when this exact change has already been delivered on this channel.

        See the ``notified_alerts`` schema comment for why this exists
        despite ``detect_changes()`` already firing a given transition only
        once: this guards delivery, not detection.
        """
        row = self._connection.execute(
            "SELECT 1 FROM notified_alerts WHERE observation_key = ? AND change_type = ? "
            "AND detected_at = ? AND run_id = ? AND channel = ?",
            (
                change.observation_key,
                change.change_type.value,
                _iso(change.detected_at),
                change.run_id or "",
                channel,
            ),
        ).fetchone()
        return row is not None

    def record_notification(
        self, change: RateChange, channel: str, notified_at: Optional[datetime] = None
    ) -> None:
        """Record that ``change`` was delivered on ``channel``, for future dedup.

        ``INSERT OR IGNORE``: recording the same (change, channel) pair twice
        (e.g. a caller re-marking after already checking :meth:`was_notified`)
        is a no-op, not an error -- this ledger only ever needs to know
        whether delivery happened at least once.
        """
        notified_at = notified_at or datetime.now(timezone.utc)
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO notified_alerts "
                "(observation_key, change_type, detected_at, run_id, channel, notified_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    change.observation_key,
                    change.change_type.value,
                    _iso(change.detected_at),
                    change.run_id or "",
                    channel,
                    _iso(notified_at),
                ),
            )

    def write_run_record(self, record: RunRecord) -> None:
        """Persist one operational run. Overwrites on a retried run_id.

        Unlike ``collection_runs``, nothing else joins against this table's
        rowid or relies on its insertion order for chronological tie-breaking,
        so a plain ``INSERT OR REPLACE`` (rather than the upsert-preserving-
        original-rowid dance ``record_observations`` uses) is sufficient.
        """
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO operational_runs "
                "(run_id, started_at, completed_at, status, product_type, banks_attempted, "
                "banks_successful, banks_failed, rates_observed, rates_changed, "
                "validation_failures, suspicious_observations, market_state, error_summary, "
                "collection_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.run_id,
                    _iso(record.started_at),
                    _iso(record.completed_at) if record.completed_at else None,
                    record.status,
                    ProductType(record.product_type).value,
                    json.dumps(list(record.banks_attempted)),
                    json.dumps(list(record.banks_successful)),
                    json.dumps(list(record.banks_failed)),
                    record.rates_observed,
                    record.rates_changed,
                    record.validation_failures,
                    record.suspicious_observations,
                    record.market_state,
                    record.error_summary,
                    record.collection_run_id,
                ),
            )

    def update_collector_health(
        self,
        bank: str,
        product_type: ProductType | str,
        *,
        success: bool,
        error: Optional[str] = None,
        at: Optional[datetime] = None,
        run_id: Optional[str] = None,
    ) -> CollectorHealth:
        """Advance one bank's consecutive-failure streak and return the new state.

        A success resets the streak to zero (a recovery, if the streak was
        previously at or above the alert threshold, is the caller's to
        detect by comparing the returned state against what
        :meth:`get_collector_health` held before this call). A failure
        increments it. Both update the corresponding ``last_*_at`` timestamp;
        ``last_alert_sent_at`` is left untouched here -- the caller
        (``orchestration.classify_health_escalation``) sets it separately,
        only once an alert is actually sent, via
        :meth:`record_collector_health_alert`.

        ``run_id``, when given and it matches this bank's stored
        ``last_processed_run_id``, means this exact operational run already
        advanced the streak -- the call is a retried ``run_operational_cycle()``
        invocation under the same ``run_id`` (e.g. crashed then re-run), not
        a new attempt. The streak is left untouched and the current state is
        returned as-is: without this, a single logical failed run retried
        several times would increment ``consecutive_failures`` once per
        retry, reaching ``alert_after_failures`` and firing a premature
        incident alert even though every other idempotent write in this run
        (``record_observations``, ``record_changes``, ``notified_alerts``)
        already treats a repeated ``run_id`` as a no-op. Omitting ``run_id``
        (the default) preserves the unconditional-advance behaviour for any
        caller not tracking operational runs.
        """
        product = ProductType(product_type)
        at = at or datetime.now(timezone.utc)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT consecutive_failures, last_processed_run_id FROM collector_health "
                "WHERE bank = ? AND product_type = ?",
                (bank, product.value),
            ).fetchone()
            if (
                existing is not None
                and run_id is not None
                and existing["last_processed_run_id"] == run_id
            ):
                return _collector_health_from_row(
                    connection.execute(
                        "SELECT * FROM collector_health WHERE bank = ? AND product_type = ?",
                        (bank, product.value),
                    ).fetchone()
                )
            previous_streak = existing["consecutive_failures"] if existing is not None else 0
            new_streak = 0 if success else previous_streak + 1
            connection.execute(
                "INSERT INTO collector_health "
                "(bank, product_type, consecutive_failures, last_success_at, last_failure_at, "
                "last_error, last_processed_run_id) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(bank, product_type) DO UPDATE SET "
                "consecutive_failures = excluded.consecutive_failures, "
                "last_success_at = COALESCE(excluded.last_success_at, collector_health.last_success_at), "
                "last_failure_at = COALESCE(excluded.last_failure_at, collector_health.last_failure_at), "
                "last_error = CASE WHEN excluded.last_error IS NOT NULL THEN excluded.last_error "
                "ELSE collector_health.last_error END, "
                "last_processed_run_id = COALESCE(excluded.last_processed_run_id, "
                "collector_health.last_processed_run_id)",
                (
                    bank,
                    product.value,
                    new_streak,
                    _iso(at) if success else None,
                    _iso(at) if not success else None,
                    error if not success else None,
                    run_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM collector_health WHERE bank = ? AND product_type = ?",
                (bank, product.value),
            ).fetchone()
        return _collector_health_from_row(row)

    def write_scenario_run(
        self,
        run_id: str,
        product_type: ProductType | str,
        term_months: int,
        scenarios: list[dict],
        generated_at: Optional[datetime] = None,
    ) -> None:
        """Persist one triggered scenario comparison for (run_id, term_months).

        ``scenarios`` is whatever JSON-serialisable structure the caller
        computed (see ``scenario_trigger.ScenarioComparison.as_dicts()``) --
        this method is deliberately agnostic to that module's dataclasses, so
        storage does not couple to the scenario-computation layer's types.
        """
        generated_at = generated_at or datetime.now(timezone.utc)
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO scenario_runs "
                "(run_id, product_type, term_months, generated_at, scenarios_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    ProductType(product_type).value,
                    term_months,
                    _iso(generated_at),
                    json.dumps(scenarios),
                ),
            )

    def get_scenario_runs(
        self, run_id: str, product_type: ProductType | str = ProductType.HOME_LOAN
    ) -> list[dict]:
        """Every triggered scenario comparison for one run, one dict per term."""
        rows = self._connection.execute(
            "SELECT term_months, generated_at, scenarios_json FROM scenario_runs "
            "WHERE run_id = ? AND product_type = ? ORDER BY term_months",
            (run_id, ProductType(product_type).value),
        ).fetchall()
        return [
            {
                "term_months": row["term_months"],
                "generated_at": _parse_iso(row["generated_at"]),
                "scenarios": json.loads(row["scenarios_json"]),
            }
            for row in rows
        ]

    def get_recent_scenario_runs(
        self, product_type: ProductType | str = ProductType.HOME_LOAN, limit: int = 20
    ) -> list[dict]:
        """Most recent triggered scenario comparisons, newest first."""
        rows = self._connection.execute(
            "SELECT run_id, term_months, generated_at, scenarios_json FROM scenario_runs "
            "WHERE product_type = ? ORDER BY generated_at DESC, id DESC LIMIT ?",
            (ProductType(product_type).value, limit),
        ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "term_months": row["term_months"],
                "generated_at": _parse_iso(row["generated_at"]),
                "scenarios": json.loads(row["scenarios_json"]),
            }
            for row in rows
        ]

    def record_collector_health_alert(
        self, bank: str, product_type: ProductType | str, at: Optional[datetime] = None
    ) -> None:
        """Record that a health escalation/recovery alert was just sent for this bank."""
        product = ProductType(product_type)
        at = at or datetime.now(timezone.utc)
        with self._transaction() as connection:
            connection.execute(
                "UPDATE collector_health SET last_alert_sent_at = ? "
                "WHERE bank = ? AND product_type = ?",
                (_iso(at), bank, product.value),
            )

    def record_collector_health_alert_channel(
        self,
        bank: str,
        product_type: ProductType | str,
        channel: str,
        at: Optional[datetime] = None,
    ) -> None:
        """Record that a health escalation alert was confirmed delivered to
        ``channel`` for this bank -- the per-channel companion to
        :meth:`record_collector_health_alert`'s bank-level column, so a
        later recovery can be limited to just the channels that actually got
        this streak's incident. See :meth:`get_collector_health_alert_channels`.
        """
        product = ProductType(product_type)
        at = at or datetime.now(timezone.utc)
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO collector_health_alert_channels "
                "(bank, product_type, channel, sent_at) VALUES (?, ?, ?, ?)",
                (bank, product.value, channel, _iso(at)),
            )

    def get_collector_health_alert_channels(
        self, bank: str, product_type: ProductType | str
    ) -> dict[str, datetime]:
        """Channel -> when a health escalation alert was last confirmed
        delivered to it for this bank, across all history.

        Not scoped to the current failure streak -- a caller deciding which
        channels to send a recovery to must filter these against the streak
        boundary itself (see
        ``orchestration._channels_delivered_during_current_streak``), the
        same way :meth:`record_collector_health_alert`'s bank-level
        ``last_alert_sent_at`` is never trusted on its own either.
        """
        product = ProductType(product_type)
        rows = self._connection.execute(
            "SELECT channel, sent_at FROM collector_health_alert_channels "
            "WHERE bank = ? AND product_type = ?",
            (bank, product.value),
        ).fetchall()
        return {row["channel"]: _parse_iso(row["sent_at"]) for row in rows}

    # ----------------------------------------------------------------- reads

    def get_run_record(self, run_id: str) -> Optional[RunRecord]:
        row = self._connection.execute(
            "SELECT * FROM operational_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return _run_record_from_row(row) if row is not None else None

    def get_latest_run_record(
        self, product_type: ProductType | str = ProductType.HOME_LOAN
    ) -> Optional[RunRecord]:
        row = self._connection.execute(
            "SELECT * FROM operational_runs WHERE product_type = ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (ProductType(product_type).value,),
        ).fetchone()
        return _run_record_from_row(row) if row is not None else None

    def get_recent_run_records(
        self, product_type: ProductType | str = ProductType.HOME_LOAN, limit: int = 10
    ) -> list[RunRecord]:
        rows = self._connection.execute(
            "SELECT * FROM operational_runs WHERE product_type = ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (ProductType(product_type).value, limit),
        ).fetchall()
        return [_run_record_from_row(row) for row in rows]

    def get_collector_health(
        self, product_type: ProductType | str = ProductType.HOME_LOAN
    ) -> list[CollectorHealth]:
        rows = self._connection.execute(
            "SELECT * FROM collector_health WHERE product_type = ? ORDER BY bank",
            (ProductType(product_type).value,),
        ).fetchall()
        return [_collector_health_from_row(row) for row in rows]

    def latest_run_id(
        self, product_type: Optional[ProductType | str] = None
    ) -> Optional[str]:
        """Resolve the most recent run, optionally scoped to one product.

        Home-loan and term-deposit collections share this one
        ``collection_runs`` table, so an unscoped call can return the other
        product's run -- ``product_type`` is what lets a caller ask for "the
        latest run *for this market*" instead of "the latest run, whichever
        market it happened to be".

        A scoped query matches a run whose stored ``product_type`` agrees
        *or* whose ``collection_run_products``/``observations``/``rate_changes``
        rows show the requested product, not the stored column alone:
        ``record_observations()`` rejects a mixed-product batch now, but a
        database from before that guard existed can still hold a legacy run
        that mixed both products under one ``run_id`` (one row, so the
        migration backfilling this column could only pick one). The extra
        checks keep such a run findable under *either* product instead of
        losing one of them -- which the stored value alone is still enough
        to resolve for the overwhelmingly common case of a normal,
        single-product run.

        ``collection_run_products`` (see :meth:`record_observations`) is
        checked first among the fallbacks because it is the only durable
        one: a mixed run whose home-loan side merely reconfirmed pre-existing
        rows produces no ``rate_changes``, and ``last_seen_run_id`` moves on
        to whatever run next touches those rows -- ``first_seen_run_id``
        would be the only surviving trace, but it was never this shared
        run's to begin with (the rows already existed before it touched
        them). It also supplies the *starting-time* half of the ordering key
        (via a ``LEFT JOIN``, not just an ``EXISTS`` match) whenever it has a
        row: a run_id reused for a second product before
        :meth:`record_observations` refused that outright (a legacy
        database only) could overwrite ``collection_runs.started_at`` with
        the second call's timestamp, retroactively misdating the *first*
        product's run if ordering fell back to that shared column --
        ``collection_run_products.started_at`` is written once, per product,
        and never touched again, so it alone still reflects when *this*
        product's data actually landed under this ``run_id``.

        The *ordering* half prefers ``collection_run_products.rowid`` too
        (via the same ``LEFT JOIN``), falling back to ``collection_runs.rowid``
        only when no ``collection_run_products`` row exists at all. Neither
        source is safe to use *unconditionally* on its own:
        ``collection_runs.rowid`` is preserved in place across every upsert
        (see the comment below) for a run_id's *first* product, but a run_id
        later reused for a *second* product via a live call -- from the
        database's transitional era between this table's introduction and
        :meth:`record_observations` refusing that outright -- has that
        second product's real position recorded only by its own
        ``collection_run_products`` row, genuinely later than some other,
        single-product run that ``collection_runs.rowid`` alone would rank
        higher. Conversely, a *migration-backfilled* row's own
        ``collection_run_products.rowid`` reflects insertion order into
        *that table*, not real history, on its own -- but
        :meth:`_migrate_collection_run_products` backfills every row in one
        sweep ordered by its source ``collection_runs.rowid``, so a
        backfilled row's freshly-assigned ``collection_run_products.rowid``
        already agrees, in relative order, with that source. Preferring
        ``collection_run_products.rowid`` whenever it exists is correct for
        both cases as a result, without this method needing to know which
        one it is looking at.

        The three ``EXISTS`` fallbacks still mirror every signal
        :meth:`_migrate_collection_runs_product_type` backfills from, kept
        as a matching net (not an ordering one -- always via
        ``collection_runs.started_at``/``rowid``) for whatever a
        pre-``collection_run_products`` database's own migration was unable
        to recover: a mixed *bootstrap* run can have zero ``rate_changes``
        for *either* product, and once both products' rows have since been
        reconfirmed unchanged, neither still shows this run via
        ``last_seen_run_id`` either -- ``observations.first_seen_run_id``
        (frozen at insertion, unlike ``last_seen_run_id``; see
        :meth:`_migrate_observations_first_seen_run_id`) is then the only
        surviving trace for the product the migration's single-column
        backfill didn't happen to pick. Correlated by that column's actual
        ``run_id``, not by matching ``first_observed_at`` against
        ``started_at`` -- two runs captured with the same timestamp (e.g. a
        caller collecting both products from one shared capture moment)
        would otherwise each appear to contain the other's rows too.
        """
        if product_type is None:
            row = self._connection.execute(
                "SELECT run_id FROM collection_runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        else:
            value = _product_value(product_type)
            row = self._connection.execute(
                "SELECT cr.run_id FROM collection_runs cr "
                "LEFT JOIN collection_run_products crp "
                "     ON crp.run_id = cr.run_id AND crp.product_type = ? "
                "WHERE cr.product_type = ? "
                "OR crp.run_id IS NOT NULL "
                "OR EXISTS (SELECT 1 FROM observations o "
                "           WHERE o.last_seen_run_id = cr.run_id AND o.product_type = ?) "
                "OR EXISTS (SELECT 1 FROM rate_changes rc "
                "           WHERE rc.run_id = cr.run_id AND rc.product_type = ?) "
                "OR EXISTS (SELECT 1 FROM observations o2 "
                "           WHERE o2.first_seen_run_id = cr.run_id AND o2.product_type = ?) "
                "ORDER BY COALESCE(crp.started_at, cr.started_at) DESC, "
                "         COALESCE(crp.rowid, cr.rowid) DESC LIMIT 1",
                (value, value, value, value, value),
            ).fetchone()
        return row["run_id"] if row else None

    def latest_collection_timestamp(
        self, product_type: ProductType | str = ProductType.HOME_LOAN
    ) -> Optional[datetime]:
        """The ``started_at`` of the most recently recorded run for this
        product, or ``None`` if none has been recorded yet.

        Same query :meth:`record_observations` uses for its own
        chronological-order guard (``ChronologicalOrderError``). Exposed so a
        caller -- :meth:`~.service.PricingIntelligenceService.collect` -- can
        fail fast on a backwards wall clock *before* fetching every bank,
        rather than only discovering it after that work is already done, when
        ``record_observations()`` finally runs. Not a replacement for that
        guard: this is a pre-check for the common case, not transaction-safe
        against a genuinely concurrent write, which the guard inside the
        write's own transaction still catches.
        """
        row = self._connection.execute(
            "SELECT MAX(started_at) FROM collection_run_products WHERE product_type = ?",
            (_product_value(product_type),),
        ).fetchone()
        return _parse_iso(row[0]) if row and row[0] is not None else None

    def get_run_started_at(self, run_id: str) -> Optional[datetime]:
        """Resolve a run's start time, for reconstructing its state via
        :meth:`get_observations_as_of` -- ``last_seen_run_id`` cannot answer
        this for an older run once later runs have touched its rows."""
        row = self._connection.execute(
            "SELECT started_at FROM collection_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return _parse_iso(row["started_at"]) if row else None

    def get_latest_rates(
        self,
        bank: Optional[str] = None,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        term_months: Optional[int] = None,
        rate_category: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> list[CompetitorRateRecord]:
        """Return the currently-advertised rates as of the latest collection run.

        Scoped to the latest run rather than to "most recent row per key", so a
        product a bank has withdrawn stops appearing.
        """
        run_id = run_id or self.latest_run_id(product_type=product_type)
        if run_id is None:
            return []
        clauses = ["last_seen_run_id = ?", "product_type = ?"]
        params: list[object] = [run_id, _product_value(product_type)]
        if bank is not None:
            clauses.append("bank = ?")
            params.append(bank)
        if term_months is not None:
            clauses.append("term_months = ?")
            params.append(term_months)
        if rate_category is not None:
            clauses.append("rate_category = ?")
            params.append(rate_category)
        rows = self._connection.execute(
            f"SELECT * FROM observations WHERE {' AND '.join(clauses)} "
            "ORDER BY bank, term_months",
            params,
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get_current_observations(
        self,
        product_type: ProductType | str = ProductType.HOME_LOAN,
    ) -> list[CompetitorRateRecord]:
        """Return the most recently known state for every observation key.

        Unlike :meth:`get_latest_rates`, this is not scoped to a single
        collection run: a bank's rows stay here even after a run that failed
        to collect it, so a later run where that bank returns unchanged reads
        as "no change" instead of "everything is new". Callers that want
        "what is on the market right now" should use :meth:`get_latest_rates`;
        this is for reconstructing the baseline change detection compares
        against.

        A key with a confirmed withdrawal (a ``REMOVED_PRODUCT`` event
        recorded after its row was last touched) is excluded, not merely
        stale: otherwise every subsequent successful collection would keep
        comparing against a row that's genuinely gone, and detect_changes()
        would emit -- and this store would persist -- the same withdrawal
        again on every run. A key merely absent because its bank failed to
        collect has no such event recorded and stays in the baseline.

        A key superseded by an eligibility edit is likewise excluded: when
        change detection pairs a current record to a previous one via
        ``eligibility_group_key`` (:func:`~.changes.resolve_pairing`), the
        previous key's row is never touched again -- its ``first_observed_at``
        stays frozen while the new key's row keeps advancing. A row sharing
        every eligibility-group field with a same-product row that first
        appeared after this one was last confirmed is that superseded state.
        Two rows that continue being reconfirmed independently (genuinely
        simultaneous LVR tiers, not a transition) are unaffected: the newer
        row's first appearance never lands after the older row's *last*
        confirmation in that case.
        """
        rows = self._connection.execute(
            "SELECT * FROM observations o WHERE product_type = ? AND id = ("
            "  SELECT id FROM observations o2 WHERE o2.observation_key = o.observation_key "
            "  ORDER BY first_observed_at DESC, id DESC LIMIT 1"
            ") AND NOT EXISTS ("
            "  SELECT 1 FROM rate_changes rc WHERE rc.observation_key = o.observation_key "
            "  AND rc.change_type = 'REMOVED_PRODUCT' AND "
            + _run_ordered_after("rc.run_id", "rc.detected_at", "o.last_seen_run_id", "o.last_seen_at")
            + ") "
            + _SUPERSEDED_BY_LATER_ELIGIBILITY_KEY.format(extra="")
            + "ORDER BY bank, term_months",
            (_product_value(product_type),),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get_current_observation_run_ids(
        self, product_type: ProductType | str = ProductType.HOME_LOAN
    ) -> dict[str, list[str]]:
        """Map each bank to the ``last_seen_run_id`` of every one of its
        current observation rows (the same rows :meth:`get_current_observations`
        returns).

        Lets a caller check whether a bank was touched by a specific run --
        e.g. the latest run for this product, via :meth:`latest_run_id` --
        by run identity rather than by comparing ``captured_at_utc``
        timestamps. Two distinct runs can share the same ``started_at``
        (recorded with second, not sub-second, precision, or simply
        launched back-to-back within the same second), which a timestamp
        comparison cannot tell apart from "the same run": a bank a later
        run failed to touch would then read as reconfirmed just because its
        surviving row's timestamp happens to match.
        """
        rows = self._connection.execute(
            "SELECT bank, last_seen_run_id FROM observations o "
            "WHERE product_type = ? AND id = ("
            "  SELECT id FROM observations o2 WHERE o2.observation_key = o.observation_key "
            "  ORDER BY first_observed_at DESC, id DESC LIMIT 1"
            ") AND NOT EXISTS ("
            "  SELECT 1 FROM rate_changes rc WHERE rc.observation_key = o.observation_key "
            "  AND rc.change_type = 'REMOVED_PRODUCT' AND "
            + _run_ordered_after("rc.run_id", "rc.detected_at", "o.last_seen_run_id", "o.last_seen_at")
            + ") "
            + _SUPERSEDED_BY_LATER_ELIGIBILITY_KEY.format(extra=""),
            (_product_value(product_type),),
        ).fetchall()
        run_ids_by_bank: dict[str, list[str]] = {}
        for row in rows:
            run_ids_by_bank.setdefault(row["bank"], []).append(row["last_seen_run_id"])
        return run_ids_by_bank

    def get_last_confirmed_observations(
        self,
        product_type: ProductType | str = ProductType.HOME_LOAN,
    ) -> list[CompetitorRateRecord]:
        """Like :meth:`get_current_observations`, but skips a "warning"-tagged
        (suspicious-but-accepted) row to find the last confirmed state per key.

        Used as the fallback when a market-derived pricing view excludes a
        suspicious record: dropping it outright would make the bank/term
        vanish from the view rather than falling back to what was last
        verified, letting a single parser fault move a MARKET_LEADER/
        MARKET_MEDIAN assumption just by making the old rate disappear.

        Also applies the eligibility-supersession exclusion (only against a
        *confirmed* superseding sibling, for the same reason as
        :meth:`get_confirmed_observations_as_of`): without it, a product that
        moved from key A to a more recent confirmed key B would offer *both*
        as fallback candidates. If a later suspicious observation happens to
        revert to A's eligibility condition, exact-key precedence in
        :func:`resolve_pairing` would then substitute the stale A instead of
        the immediately preceding B -- itself a market-moving substitution,
        just a different wrong one.
        """
        rows = self._connection.execute(
            "SELECT * FROM observations o WHERE product_type = ? "
            "AND validation_status != 'warning' AND id = ("
            "  SELECT id FROM observations o2 WHERE o2.observation_key = o.observation_key "
            "  AND o2.validation_status != 'warning' "
            "  ORDER BY first_observed_at DESC, id DESC LIMIT 1"
            ") AND NOT EXISTS ("
            "  SELECT 1 FROM rate_changes rc WHERE rc.observation_key = o.observation_key "
            "  AND rc.change_type = 'REMOVED_PRODUCT' AND "
            + _run_ordered_after("rc.run_id", "rc.detected_at", "o.last_seen_run_id", "o.last_seen_at")
            + ") "
            + _SUPERSEDED_BY_LATER_ELIGIBILITY_KEY.format(
                extra=" AND o3.validation_status != 'warning'"
            )
            + "ORDER BY bank, term_months",
            (_product_value(product_type),),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get_rate_history(
        self,
        bank: Optional[str] = None,
        term_months: Optional[int] = None,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        rate_category: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[CompetitorRateRecord]:
        """Return every distinct advertised state matching the filters, oldest first."""
        clauses = ["product_type = ?"]
        params: list[object] = [_product_value(product_type)]
        if bank is not None:
            clauses.append("bank = ?")
            params.append(bank)
        if term_months is not None:
            clauses.append("term_months = ?")
            params.append(term_months)
        if rate_category is not None:
            clauses.append("rate_category = ?")
            params.append(rate_category)
        if since is not None:
            clauses.append("last_seen_at >= ?")
            params.append(_iso(since))
        if until is not None:
            clauses.append("first_observed_at <= ?")
            params.append(_iso(until))
        rows = self._connection.execute(
            f"SELECT * FROM observations WHERE {' AND '.join(clauses)} "
            "ORDER BY first_observed_at ASC, id ASC",
            params,
        ).fetchall()
        return [_row_to_history_record(row) for row in rows]

    def _as_of_cutoff(
        self, before_run_id: Optional[str], timestamp: str
    ) -> Optional["_AsOfCutoff"]:
        """Build the SQL cutoff fragments and params for
        :meth:`get_observations_as_of`/:meth:`get_confirmed_observations_as_of`.

        Returns ``None`` when ``before_run_id`` was given but no longer
        resolves to a stored run (an empty result is then correct, not an
        unscoped fallback).

        Plain ``first_observed_at <= ?`` / ``rc.detected_at <= ?`` when
        ``before_run_id`` is omitted -- unchanged, timestamp-only behaviour.
        When given, anchors every cutoff to that specific run's
        ``(started_at, rowid)`` position instead: two runs for the same
        product can share an identical ``started_at``
        (:meth:`record_observations`'s public API allows an explicit
        ``observed_at``), and a plain timestamp cutoff cannot tell which of
        them a reconstruction should stop at -- a row (or a withdrawal event,
        or an eligibility-superseding sibling) introduced by a *later*
        same-timestamp run would still satisfy ``<= as_of`` (they're equal)
        and either win the per-key selection, retroactively apply a
        withdrawal, or retroactively supersede a row one run early, even when
        the caller specifically wants the earlier run's state (see
        :meth:`get_previous_run_rates`). ``outer``/``inner``/``eligibility``
        all anchor via an observation row's ``first_seen_run_id``;
        ``withdrawal`` anchors via ``rate_changes.run_id`` instead, since a
        ``REMOVED_PRODUCT`` event needs the same run-order treatment as the
        observation rows it excludes -- with a further fallback to
        ``rc.detected_at`` for a ``rate_changes`` row whose ``run_id`` does
        not correlate to any stored run at all (e.g. one written via
        ``record_changes()`` from a caller that built its own ``RateChange``
        without a real ``run_id`` -- :meth:`record_changes` stores that as
        ``""``, not ``NULL``): such a row has no run position to anchor to,
        so it falls back to being honoured by timestamp alone rather than
        silently never excluding anything.
        """
        if before_run_id is None:
            return _AsOfCutoff(
                outer="o.first_observed_at <= ?",
                inner="o2.first_observed_at <= ?",
                eligibility="o3.first_observed_at <= ?",
                withdrawal="rc.detected_at <= ?",
                observation_params=(timestamp,),
                withdrawal_params=(timestamp,),
            )
        reference = self._connection.execute(
            "SELECT started_at, rowid FROM collection_runs WHERE run_id = ?",
            (before_run_id,),
        ).fetchone()
        if reference is None:
            return None
        observation_params = (reference["started_at"], reference["started_at"], reference["rowid"])
        ordering = "(cr_row.started_at < ? OR (cr_row.started_at = ? AND cr_row.rowid <= ?))"
        observation_cutoff = (
            f"EXISTS (SELECT 1 FROM collection_runs cr_row WHERE cr_row.run_id = {{alias}}.first_seen_run_id "
            f"AND {ordering})"
        )
        withdrawal_cutoff = (
            f"(EXISTS (SELECT 1 FROM collection_runs cr_row WHERE cr_row.run_id = rc.run_id AND {ordering}) "
            "OR (NOT EXISTS (SELECT 1 FROM collection_runs cr_check WHERE cr_check.run_id = rc.run_id) "
            "AND rc.detected_at <= ?))"
        )
        return _AsOfCutoff(
            outer=observation_cutoff.format(alias="o"),
            inner=observation_cutoff.format(alias="o2"),
            eligibility=observation_cutoff.format(alias="o3"),
            withdrawal=withdrawal_cutoff,
            observation_params=observation_params,
            withdrawal_params=(*observation_params, timestamp),
        )

    def get_observations_as_of(
        self,
        as_of: datetime,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        before_run_id: Optional[str] = None,
    ) -> list[CompetitorRateRecord]:
        """Reconstruct the advertised state of every key as of a given time.

        ``last_seen_run_id`` is unsuitable for this: it is overwritten forward
        on every unchanged re-observation, so it cannot say what a key looked
        like at an earlier run once a later run has touched it. This instead
        picks, per key, the most recent row whose ``first_observed_at`` is at
        or before ``as_of`` -- the state that was actually current then.

        ``before_run_id``, when given, anchors that per-key selection to a
        specific run's position rather than ``as_of`` alone -- see
        :meth:`_as_of_cutoff` for why a timestamp alone can be ambiguous.

        A key withdrawn before ``as_of`` and never re-added must not be
        resurrected just because its last row predates the cutoff. The
        ``rate_changes`` audit log (written whenever a collection detects and
        records changes) is the source of truth for when a key was pulled: a
        key is excluded when a ``REMOVED_PRODUCT`` event for it landed between
        the selected row's start and ``as_of``. A withdrawal recorded before
        that row started does not apply -- the product was re-added since.

        Each returned record is dated from its state's ``first_observed_at``
        (like :meth:`get_rate_history`), not the row's ``last_seen_at`` --
        which a later, still-unchanged collection can push past ``as_of``
        entirely, putting a future timestamp on a reconstruction of the past.

        A key superseded by an eligibility edit by ``as_of`` is likewise
        excluded (see :meth:`get_current_observations`), bounded so an edit
        that happens *after* ``as_of`` does not retroactively exclude the
        predecessor from a reconstruction of an earlier point in time.
        """
        timestamp = _iso(as_of)
        cutoff = self._as_of_cutoff(before_run_id, timestamp)
        if cutoff is None:
            return []
        rows = self._connection.execute(
            f"SELECT * FROM observations o WHERE product_type = ? AND {cutoff.outer} "
            "AND id = ("
            f"  SELECT id FROM observations o2 WHERE o2.observation_key = o.observation_key "
            f"  AND {cutoff.inner} ORDER BY first_observed_at DESC, id DESC LIMIT 1"
            ") "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM rate_changes rc WHERE rc.observation_key = o.observation_key "
            f"  AND rc.change_type = 'REMOVED_PRODUCT' AND {cutoff.withdrawal} "
            "  AND "
            + _run_ordered_after("rc.run_id", "rc.detected_at", "o.first_seen_run_id", "o.first_observed_at")
            + ") "
            + _SUPERSEDED_BY_LATER_ELIGIBILITY_KEY.format(extra=f" AND {cutoff.eligibility}")
            + "ORDER BY bank, term_months",
            (
                _product_value(product_type),
                *cutoff.observation_params,
                *cutoff.observation_params,
                *cutoff.withdrawal_params,
                *cutoff.observation_params,
            ),
        ).fetchall()
        return [_row_to_history_record(row) for row in rows]

    def get_confirmed_observations_as_of(
        self,
        as_of: datetime,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        before_run_id: Optional[str] = None,
    ) -> list[CompetitorRateRecord]:
        """Like :meth:`get_observations_as_of`, but skips a "warning"-tagged
        row to find the last confirmed state per key as of a point in time.

        Used as the historical counterpart to :meth:`get_last_confirmed_observations`:
        a market-derived view of an older run (``get_previous_market_snapshot()``,
        or ``get_market_snapshot(as_of_run_id=...)``) must fall back to what
        was last verified as of that point rather than dropping a
        suspicious-tagged bank/term from the reconstruction outright.

        The eligibility-supersession exclusion only considers a *confirmed*
        sibling key: a warning-tagged sibling is precisely the unreliable
        state a caller is falling back away from, not a confirmed transition
        that should retire this row's candidacy as the fallback.
        """
        timestamp = _iso(as_of)
        cutoff = self._as_of_cutoff(before_run_id, timestamp)
        if cutoff is None:
            return []
        rows = self._connection.execute(
            f"SELECT * FROM observations o WHERE product_type = ? AND {cutoff.outer} "
            "AND validation_status != 'warning' "
            "AND id = ("
            f"  SELECT id FROM observations o2 WHERE o2.observation_key = o.observation_key "
            f"  AND {cutoff.inner} AND o2.validation_status != 'warning' "
            "  ORDER BY first_observed_at DESC, id DESC LIMIT 1"
            ") "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM rate_changes rc WHERE rc.observation_key = o.observation_key "
            f"  AND rc.change_type = 'REMOVED_PRODUCT' AND {cutoff.withdrawal} "
            "  AND "
            + _run_ordered_after("rc.run_id", "rc.detected_at", "o.first_seen_run_id", "o.first_observed_at")
            + ") "
            + _SUPERSEDED_BY_LATER_ELIGIBILITY_KEY.format(
                extra=f" AND {cutoff.eligibility} AND o3.validation_status != 'warning'"
            )
            + "ORDER BY bank, term_months",
            (
                _product_value(product_type),
                *cutoff.observation_params,
                *cutoff.observation_params,
                *cutoff.withdrawal_params,
                *cutoff.observation_params,
            ),
        ).fetchall()
        return [_row_to_history_record(row) for row in rows]

    def _previous_run_row(
        self,
        before_run_id: Optional[str],
        product_type: Optional[ProductType | str],
    ) -> Optional[sqlite3.Row]:
        """Resolve the run immediately before ``before_run_id`` (default: the
        latest run), optionally scoped to one product.

        Shared by :meth:`get_previous_run_started_at` (which only needs the
        timestamp) and :meth:`get_previous_run_rates` (which also needs the
        ``run_id`` to anchor :meth:`get_observations_as_of`'s reconstruction
        to this specific run rather than to its timestamp alone -- see that
        method's own docstring for why the two are not interchangeable when
        two runs share a timestamp).
        """
        before_run_id = before_run_id or self.latest_run_id(product_type=product_type)
        if before_run_id is None:
            return None
        if product_type is None:
            reference = self._connection.execute(
                "SELECT started_at, rowid FROM collection_runs WHERE run_id = ?",
                (before_run_id,),
            ).fetchone()
        else:
            # Resolved the same product-aware way as the main query below --
            # reference_started_at/reference_rowid anchor the "precedes"
            # comparison, so if before_run_id is itself a run_id later
            # reused for a second product, its collection_runs.started_at
            # may already reflect that later call rather than this
            # product's own original timing (see the main query's comment).
            reference = self._connection.execute(
                "SELECT COALESCE(crp.started_at, cr.started_at) AS started_at, "
                "COALESCE(crp.rowid, cr.rowid) AS rowid "
                "FROM collection_runs cr "
                "LEFT JOIN collection_run_products crp "
                "     ON crp.run_id = cr.run_id AND crp.product_type = ? "
                "WHERE cr.run_id = ?",
                (_product_value(product_type), before_run_id),
            ).fetchone()
        if reference is None:
            return None
        # Two runs for the same product can share started_at -- record_observations()'s
        # public API allows an explicit observed_at, and latest_run_id() already
        # breaks such ties on rowid. Matching only "started_at < reference" (not
        # this same (started_at, rowid) pair) would treat a same-timestamp
        # earlier-inserted run as not preceding the reference at all, skipping it
        # entirely rather than returning it as the correct predecessor.
        reference_started_at = reference["started_at"]
        reference_rowid = reference["rowid"]
        if product_type is None:
            precedes = "(cr.started_at < ? OR (cr.started_at = ? AND cr.rowid < ?))"
            return self._connection.execute(
                f"SELECT run_id, started_at FROM collection_runs cr WHERE {precedes} "
                "ORDER BY cr.started_at DESC, cr.rowid DESC LIMIT 1",
                (reference_started_at, reference_started_at, reference_rowid),
            ).fetchone()
        # Same stored-value-or-EXISTS matching as latest_run_id(), for the
        # same reason: a legacy run that mixed both products under one
        # run_id (before record_observations() rejected that) must stay
        # findable under either product, not just whichever one the
        # migration's single-column backfill happened to pick.
        # collection_run_products (see record_observations()) supplies both
        # the match and (via the LEFT JOIN/COALESCE) the ordering timing as
        # the only durable signal -- a mixed run whose side for this product
        # merely reconfirmed pre-existing rows leaves none of the other
        # three behind once a later run moves last_seen_run_id on, and
        # collection_runs.started_at/rowid themselves may already belong to
        # a second product that later reused this run_id. See
        # latest_run_id()'s docstring for why preferring
        # collection_run_products' own rowid (falling back to
        # collection_runs.rowid only when no such row exists) is correct for
        # both a migration-backfilled row (backfilled in one sweep ordered
        # by its source collection_runs.rowid, so the two agree in relative
        # order) and a live-reused run_id from this store's transitional era
        # (whose real position collection_runs.rowid alone cannot capture).
        # Those three EXISTS checks remain as a matching net (not an
        # ordering one) for whatever a pre-collection_run_products
        # database's own migration (_migrate_collection_run_products())
        # could not recover: every signal
        # _migrate_collection_runs_product_type() backfills from, including
        # a mixed bootstrap run's only surviving trace, first_seen_run_id
        # (correlated by actual run_id, like last_seen_run_id/
        # rate_changes.run_id above -- not by matching first_observed_at
        # against started_at, which would also match an unrelated run
        # captured at the same timestamp).
        value = _product_value(product_type)
        precedes = (
            "(COALESCE(crp.started_at, cr.started_at) < ? "
            "OR (COALESCE(crp.started_at, cr.started_at) = ? "
            "    AND COALESCE(crp.rowid, cr.rowid) < ?))"
        )
        return self._connection.execute(
            "SELECT cr.run_id AS run_id, "
            "COALESCE(crp.started_at, cr.started_at) AS started_at "
            "FROM collection_runs cr "
            "LEFT JOIN collection_run_products crp "
            f"     ON crp.run_id = cr.run_id AND crp.product_type = ? WHERE {precedes} "
            "AND (cr.product_type = ? "
            "     OR crp.run_id IS NOT NULL "
            "     OR EXISTS (SELECT 1 FROM observations o "
            "                WHERE o.last_seen_run_id = cr.run_id AND o.product_type = ?) "
            "     OR EXISTS (SELECT 1 FROM rate_changes rc "
            "                WHERE rc.run_id = cr.run_id AND rc.product_type = ?) "
            "     OR EXISTS (SELECT 1 FROM observations o2 "
            "                WHERE o2.first_seen_run_id = cr.run_id AND o2.product_type = ?)) "
            "ORDER BY COALESCE(crp.started_at, cr.started_at) DESC, "
            "         COALESCE(crp.rowid, cr.rowid) DESC LIMIT 1",
            (
                value,
                reference_started_at,
                reference_started_at,
                reference_rowid,
                value,
                value,
                value,
                value,
            ),
        ).fetchone()

    def get_previous_run_started_at(
        self,
        before_run_id: Optional[str] = None,
        product_type: Optional[ProductType | str] = None,
    ) -> Optional[datetime]:
        """Resolve the start time of the run before ``before_run_id``
        (default: the latest run), optionally scoped to one product.

        Home-loan and term-deposit collections share this one
        ``collection_runs`` table, so an unscoped call can resolve either
        endpoint -- the default "latest run" or the "previous" run found from
        it -- to the other product's run. ``product_type`` is what lets a
        caller ask for the run before the latest *for this market*, e.g. so a
        deposit run sandwiched between two mortgage runs is not skipped over
        (or, without scoping ``before_run_id`` itself, mistaken for the
        mortgage market's own previous state).

        Exposed separately from :meth:`get_previous_run_rates` so a caller can
        also pass this timestamp on as the snapshot's ``as_of``, rather than
        letting it be inferred from the reconstructed records themselves.
        """
        previous = self._previous_run_row(before_run_id, product_type)
        return _parse_iso(previous["started_at"]) if previous is not None else None

    def get_previous_run_id(
        self,
        before_run_id: Optional[str] = None,
        product_type: Optional[ProductType | str] = None,
    ) -> Optional[str]:
        """Resolve the ``run_id`` of the run before ``before_run_id``
        (default: the latest run), optionally scoped to one product.

        A caller reconstructing that run's state (e.g.
        ``get_previous_market_snapshot()``) needs this alongside
        :meth:`get_previous_run_started_at`'s timestamp, to pass on as
        :meth:`get_observations_as_of`'s ``before_run_id`` -- see that
        method's docstring for why the timestamp alone is not always enough
        to identify the right run when two runs share a timestamp.

        Calling this and :meth:`get_previous_run_started_at` as two separate
        store round-trips is unsafe when both values are needed for the
        *same* reconstruction: a collection commit landing between the two
        calls can shift which run is "the latest", so each call resolves
        "the previous run" relative to a different reference and returns a
        run_id/started_at pair that do not name the same run. Use
        :meth:`get_previous_run` instead for that case.
        """
        previous = self._previous_run_row(before_run_id, product_type)
        return previous["run_id"] if previous is not None else None

    def get_previous_run(
        self,
        before_run_id: Optional[str] = None,
        product_type: Optional[ProductType | str] = None,
    ) -> Optional[PreviousRun]:
        """Resolve the run before ``before_run_id`` (default: the latest
        run), returning its ``run_id`` and ``started_at`` together from one
        lookup.

        :meth:`get_previous_run_started_at` and :meth:`get_previous_run_id`
        each independently call :meth:`_previous_run_row`. A caller needing
        both values for one reconstruction (e.g.
        :meth:`~.service.PricingIntelligenceService.get_previous_market_snapshot`,
        which anchors :meth:`get_observations_as_of` to both the timestamp
        *and* the run_id) must not call them as two separate store
        round-trips: another same-product collection committing between the
        two calls shifts which run each one resolves "previous" relative to,
        so the resulting timestamp and run_id can name two different runs --
        the snapshot would then reconstruct one run's state while labelling
        it with another run's ``as_of``. This method resolves both from a
        single :meth:`_previous_run_row` call, so they always name the same
        run.
        """
        previous = self._previous_run_row(before_run_id, product_type)
        if previous is None:
            return None
        return PreviousRun(run_id=previous["run_id"], started_at=_parse_iso(previous["started_at"]))

    def get_previous_run_rates(
        self,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        before_run_id: Optional[str] = None,
    ) -> list[CompetitorRateRecord]:
        """Return the advertised rates from the run before ``before_run_id``."""
        previous_run = self._previous_run_row(before_run_id, product_type)
        if previous_run is None:
            return []
        return self.get_observations_as_of(
            as_of=_parse_iso(previous_run["started_at"]),
            product_type=product_type,
            before_run_id=previous_run["run_id"],
        )

    def get_changes_since(
        self,
        since: Optional[datetime] = None,
        days: Optional[int] = None,
        bank: Optional[str] = None,
        term_months: Optional[int] = None,
        include_suspicious: bool = True,
        now: Optional[datetime] = None,
        product_type: Optional[ProductType | str] = None,
        rate_category: Optional[str] = None,
    ) -> list[dict]:
        """Return stored change events in a window, newest first.

        Returns plain dicts; ``changes.load_changes`` rehydrates them into
        ``RateChange`` objects for callers that want the typed form.
        """
        now = now or datetime.now(timezone.utc)
        if since is None and days is not None:
            since = now - timedelta(days=days)
        # Bounds the upper edge too: a historical now= must not surface a
        # change detected after that point, the same as the lower (since)
        # edge already does.
        clauses: list[str] = ["detected_at <= ?"]
        params: list[object] = [_iso(now)]
        if since is not None:
            clauses.append("detected_at >= ?")
            params.append(_iso(since))
        if bank is not None:
            clauses.append("bank = ?")
            params.append(bank)
        if term_months is not None:
            clauses.append("term_months = ?")
            params.append(term_months)
        if product_type is not None:
            clauses.append("product_type = ?")
            params.append(_product_value(product_type))
        if rate_category is not None:
            clauses.append("rate_category = ?")
            params.append(rate_category)
        if not include_suspicious:
            clauses.append("suspicious = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM rate_changes {where} ORDER BY detected_at DESC, id DESC", params
        ).fetchall()
        return [dict(row) for row in rows]

    def get_banks(self, product_type: ProductType | str = ProductType.HOME_LOAN) -> list[str]:
        rows = self._connection.execute(
            "SELECT DISTINCT bank FROM observations WHERE product_type = ? ORDER BY bank",
            (_product_value(product_type),),
        ).fetchall()
        return [row["bank"] for row in rows]

    def get_terms(self, product_type: ProductType | str = ProductType.HOME_LOAN) -> list[int]:
        rows = self._connection.execute(
            "SELECT DISTINCT term_months FROM observations "
            "WHERE product_type = ? AND term_months IS NOT NULL ORDER BY term_months",
            (_product_value(product_type),),
        ).fetchall()
        return [row["term_months"] for row in rows]

    def check_integrity(self, thresholds: Optional[ValidationThresholds] = None) -> IntegrityReport:
        """Lightweight, read-only database health check for the ``health`` CLI command.

        Deliberately narrow -- this is an operational smoke check, not a
        replacement for ``PRAGMA integrity_check`` (which it also runs) or a
        full re-validation pass. ``no_duplicate_current_keys`` checks
        :meth:`get_current_observations`'s own result rather than the raw
        ``observations`` table: several rows legitimately share one
        ``observation_key`` across history (one per distinct advertised
        state), so the invariant that matters is "at most one *current* row
        per key", which only that method's own current-state logic can
        answer correctly.
        """
        thresholds = thresholds or ValidationThresholds()
        issues: list[str] = []
        checks: dict[str, bool] = {}

        pragma_row = self._connection.execute("PRAGMA integrity_check").fetchone()
        schema_ok = pragma_row is not None and pragma_row[0] == "ok"
        checks["schema_accessible"] = schema_ok
        if not schema_ok:
            issues.append(f"PRAGMA integrity_check: {pragma_row[0] if pragma_row else 'no result'}")

        # Both products, not get_current_observations()'s own default
        # (HOME_LOAN only): a malformed stored row (e.g. an unparseable
        # payment-frequency field) that only exists on the term-deposit side
        # would otherwise never surface here, and this command could exit
        # successfully while the deposit snapshot is actually unusable.
        current: list[CompetitorRateRecord] = []
        readable = True
        unreadable_errors: list[str] = []
        for product in (ProductType.HOME_LOAN, ProductType.TERM_DEPOSIT):
            try:
                current.extend(self.get_current_observations(product))
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                readable = False
                unreadable_errors.append(f"{product.value}: {exc}")
        checks["latest_observations_readable"] = readable
        if unreadable_errors:
            issues.append("latest observations unreadable: " + "; ".join(unreadable_errors))

        keys = [(record.observation_key(), record.product_type) for record in current]
        checks["no_duplicate_current_keys"] = len(keys) == len(set(keys))
        if not checks["no_duplicate_current_keys"]:
            issues.append("more than one current row shares the same observation_key")

        # Queried per product_type, each against its own bounds -- collapsing
        # loan and deposit thresholds into their widest union (as a single
        # combined query would) lets a corrupted rate on one product hide
        # behind the other's wider band: with the defaults, a 0.1% home-loan
        # rate would pass because 0% is a valid deposit minimum, even though
        # ordinary ingestion validation (which selects bounds by
        # product_type) would reject it outright.
        product_bounds = {
            ProductType.HOME_LOAN: (
                thresholds.min_mortgage_rate_pct,
                thresholds.max_mortgage_rate_pct,
            ),
            ProductType.TERM_DEPOSIT: (
                thresholds.min_deposit_rate_pct,
                thresholds.max_deposit_rate_pct,
            ),
        }
        bad_rate_count = 0
        for product, (min_rate, max_rate) in product_bounds.items():
            bad_rate_count += self._connection.execute(
                "SELECT COUNT(*) FROM observations "
                "WHERE product_type = ? AND (rate_pct < ? OR rate_pct > ?)",
                (product.value, min_rate, max_rate),
            ).fetchone()[0]
        checks["rates_within_plausible_bounds"] = bad_rate_count == 0
        if bad_rate_count:
            issues.append(f"{bad_rate_count} stored observation(s) fall outside plausible rate bounds")

        broken_timestamps = 0
        for row in self._connection.execute(
            "SELECT first_observed_at, last_seen_at FROM observations"
        ):
            for value in row:
                try:
                    _parse_iso(value)
                except (ValueError, TypeError):
                    broken_timestamps += 1
        checks["timestamps_parse"] = broken_timestamps == 0
        if broken_timestamps:
            issues.append(f"{broken_timestamps} stored timestamp(s) failed to parse")

        return IntegrityReport(ok=all(checks.values()), checks=checks, issues=tuple(issues))


def _product_value(product_type: ProductType | str) -> str:
    return ProductType(product_type).value


def _is_unchanged(current: sqlite3.Row, payload: dict) -> bool:
    """True when the new observation matches the stored state on material fields."""
    for field_name in _MATERIAL_FIELDS:
        stored = current[field_name]
        incoming = payload[field_name]
        if isinstance(stored, float) or isinstance(incoming, float):
            if stored is None or incoming is None:
                if stored is not incoming:
                    return False
                continue
            # Advertised rates are quoted to 3dp of a percent; compare at that
            # precision so a float round-trip through SQLite is not a "change".
            if abs(float(stored) - float(incoming)) > 5e-7:
                return False
            continue
        if stored != incoming:
            return False
    return True


def _record_to_row(record: CompetitorRateRecord, timestamp: str, run_id: str) -> dict:
    return {
        "observation_key": record.observation_key(),
        "bank": record.bank,
        "product_type": _product_value(record.product_type),
        "product_name": record.product_name,
        "rate_category": record.rate_category,
        "term_months": record.term_months_normalised,
        "term_label_raw": record.term_label_raw,
        "term_days_raw": record.term_days_raw,
        "rate_pct": record.advertised_rate_pct,
        "interest_payment_frequency": record.interest_payment_frequency,
        "min_balance": record.min_balance_or_deposit,
        "max_balance": record.max_balance_or_deposit,
        "lvr_condition": record.lvr_condition,
        "max_lvr": record.max_lvr,
        "min_equity": record.min_equity,
        "new_business_only": (
            None if record.new_business_only is None else int(record.new_business_only)
        ),
        "cashback_text": record.cashback_text,
        "promotion_text": record.promotion_text,
        "package_requirement": record.package_requirement,
        "eligibility_text": record.special_conditions,
        "source_url": record.source_url,
        "source_page_title": record.source_bank_page_title,
        "source_text": record.raw_text_context,
        "effective_date_text": record.effective_date_text,
        "content_hash": record.raw_html_sha256,
        "parser_version": record.parser_version,
        "validation_status": record.validation_status,
        "validation_warnings": json.dumps(record.validation_warnings),
        "first_observed_at": timestamp,
        "first_seen_run_id": run_id,
        "last_seen_at": timestamp,
        "last_seen_run_id": run_id,
        "observation_count": 1,
    }


def _row_to_record(row: sqlite3.Row) -> CompetitorRateRecord:
    return CompetitorRateRecord(
        bank=row["bank"],
        product_type=ProductType(row["product_type"]),
        product_name=row["product_name"],
        rate_category=row["rate_category"],
        term_label_raw=row["term_label_raw"],
        term_months_normalised=row["term_months"],
        term_days_raw=row["term_days_raw"],
        advertised_rate_pct=row["rate_pct"],
        interest_payment_frequency=row["interest_payment_frequency"],
        min_balance_or_deposit=row["min_balance"],
        max_balance_or_deposit=row["max_balance"],
        lvr_condition=row["lvr_condition"],
        max_lvr=row["max_lvr"],
        min_equity=row["min_equity"],
        new_business_only=(
            None if row["new_business_only"] is None else bool(row["new_business_only"])
        ),
        cashback_text=row["cashback_text"],
        promotion_text=row["promotion_text"],
        package_requirement=row["package_requirement"],
        special_conditions=row["eligibility_text"],
        source_url=row["source_url"],
        source_bank_page_title=row["source_page_title"],
        captured_at_utc=_parse_iso(row["last_seen_at"]),
        effective_date_text=row["effective_date_text"],
        raw_text_context=row["source_text"],
        raw_html_sha256=row["content_hash"],
        parser_version=row["parser_version"],
        validation_status=row["validation_status"],
        validation_warnings=json.loads(row["validation_warnings"]),
    )


def record_first_observed_at(row: sqlite3.Row) -> datetime:
    """Expose a row's first-observed timestamp (used by history rendering)."""
    return _parse_iso(row["first_observed_at"])


# Placeholder used by _row_to_history_record() in place of a stale-or-wrong
# evidence hash it cannot honestly reconstruct -- 64 zero characters passes
# CompetitorRateRecord's raw_html_sha256 length validation (a real SHA-256
# is never all zeros) while being unmistakably a sentinel to a reader, not a
# real digest that happens to look suspicious.
_NO_HISTORICAL_EVIDENCE_HASH = "0" * 64
_NO_HISTORICAL_EVIDENCE_TEXT = (
    "(evidence unavailable: this state's stored evidence has since been "
    "refreshed by a later, materially-unchanged collection and no longer "
    "reflects this point in time)"
)


def _row_to_history_record(row: sqlite3.Row) -> CompetitorRateRecord:
    """Like :func:`_row_to_record`, but dates the state from when it began.

    History must reflect how long a state actually held, not when it was
    last reconfirmed: an unchanged state's ``last_seen_at`` creeps forward on
    every identical collection, which would otherwise make the dashboard lose
    a state's true start date.

    ``raw_text_context``/``raw_html_sha256``/``parser_version`` are replaced
    with placeholders for the same underlying reason: an unchanged reading's
    touch (see ``record_observations()``) refreshes those columns on the
    collapsed row in place, so by the time a later touch happens they reflect
    evidence from whichever collection most recently confirmed the state --
    not what was actually seen back when this reconstruction's point in time
    was current. Returning that evidence under a historical timestamp would
    be actively misleading (evidence *from the future* relative to the
    reconstructed point), not merely stale, so a clearly-labelled placeholder
    is returned instead of a claim this store cannot actually stand behind.
    ``source_url``/``source_bank_page_title`` are left as the current values:
    unlike page content, a bank's URL rarely changes, and the still-current
    canonical link is more useful here than a placeholder.
    """
    return _row_to_record(row).model_copy(
        update={
            "captured_at_utc": record_first_observed_at(row),
            "raw_text_context": _NO_HISTORICAL_EVIDENCE_TEXT,
            "raw_html_sha256": _NO_HISTORICAL_EVIDENCE_HASH,
            "parser_version": "unavailable",
        }
    )
