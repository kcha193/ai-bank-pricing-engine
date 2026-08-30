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
from pathlib import Path
import sqlite3
from typing import Iterable, Iterator, Mapping, NamedTuple, Optional, Sequence
import uuid

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.changes import ChangeType

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

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
    UNIQUE (observation_key, change_type, detected_at)
);

CREATE INDEX IF NOT EXISTS idx_changes_detected_at ON rate_changes (detected_at);
"""

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


@dataclass(frozen=True)
class WriteResult:
    """Outcome of persisting one collection run."""

    run_id: str
    inserted: int
    unchanged: int

    @property
    def total(self) -> int:
        return self.inserted + self.unchanged


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


class ObservationStore:
    """Repository over historical rate observations and detected changes."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        # A single shared connection keeps an in-memory store usable across
        # calls, which is what the tests and the CLI both want.
        self._connection = sqlite3.connect(self.database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._transaction_depth = 0
        self._create_schema()

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        self._connection.close()

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
            row = connection.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )

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
           that ``detected_at`` (``rate_changes`` is unique on
           ``(observation_key, change_type, detected_at)``). Without this
           restriction the correlated subquery matches *both* tied rows and
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

    # ----------------------------------------------------------------- writes

    def record_observations(
        self,
        records: Iterable[CompetitorRateRecord],
        run_id: Optional[str] = None,
        observed_at: Optional[datetime] = None,
        report_json: Optional[str] = None,
    ) -> WriteResult:
        """Persist one collection run, collapsing unchanged re-observations.

        Returns counts of newly-inserted advertised states versus unchanged
        re-observations, so a caller can tell a quiet day from a repricing day
        without querying further.

        ``collection_runs.product_type`` is taken from the records themselves,
        defaulting to ``HOME_LOAN`` for an empty ``records`` so a run is still
        recorded consistently with this store's pre-term-deposit history. This
        is what lets :meth:`latest_run_id`/:meth:`get_previous_run_started_at`
        resolve "the latest run" *for a given product* now that home-loan and
        term-deposit collections share one ``collection_runs`` table -- which
        only works if every record in one call belongs to the same product:
        :meth:`~.service.PricingIntelligenceService.collect` always calls this
        with one product's own ``accepted`` list, and a caller seeding a store
        directly must do the same, one call per product, rather than passing
        a mixed batch that only one product's runs could ever be found under.
        """
        records = list(records)
        observed_at = observed_at or datetime.now(timezone.utc)
        run_id = run_id or str(uuid.uuid4())
        timestamp = _iso(observed_at)
        product_types = {ProductType(record.product_type) for record in records}
        if len(product_types) > 1:
            raise ValueError(
                "record_observations() received a mixed-product batch "
                f"({sorted(p.value for p in product_types)}); one run must be a single "
                "product's collection -- call it once per product instead."
            )
        product_type = next(iter(product_types), ProductType.HOME_LOAN)

        inserted = 0
        unchanged = 0
        banks = sorted({record.bank for record in records})

        with self._transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO collection_runs "
                "(run_id, started_at, banks, product_type, record_count, inserted_count, "
                "unchanged_count, report_json) VALUES (?, ?, ?, ?, ?, 0, 0, ?)",
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

        logger.info(
            "recorded run %s: %s records (%s new states, %s unchanged)",
            run_id,
            len(records),
            inserted,
            unchanged,
        )
        return WriteResult(run_id=run_id, inserted=inserted, unchanged=unchanged)

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
        """
        written = 0
        with self._transaction() as connection:
            for change in changes:
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
                        change.run_id or "",
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

    # ----------------------------------------------------------------- reads

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
        *or* whose ``observations``/``rate_changes`` rows show the requested
        product, not the stored column alone: ``record_observations()``
        rejects a mixed-product batch now, but a database from before that
        guard existed can still hold a legacy run that mixed both products
        under one ``run_id`` (one row, so the migration backfilling this
        column could only pick one). The extra check keeps such a run
        findable under *either* product instead of losing one of them --
        which the stored value alone is still enough to resolve for the
        overwhelmingly common case of a normal, single-product run.

        Mirrors all three signals :meth:`_migrate_collection_runs_product_type`
        backfills from, not just two of them: a mixed *bootstrap* run can have
        zero ``rate_changes`` for *either* product, and once both products'
        rows have since been reconfirmed unchanged, neither still shows this
        run via ``last_seen_run_id`` either -- ``observations.first_seen_run_id``
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
                "SELECT cr.run_id FROM collection_runs cr WHERE cr.product_type = ? "
                "OR EXISTS (SELECT 1 FROM observations o "
                "           WHERE o.last_seen_run_id = cr.run_id AND o.product_type = ?) "
                "OR EXISTS (SELECT 1 FROM rate_changes rc "
                "           WHERE rc.run_id = cr.run_id AND rc.product_type = ?) "
                "OR EXISTS (SELECT 1 FROM observations o2 "
                "           WHERE o2.first_seen_run_id = cr.run_id AND o2.product_type = ?) "
                "ORDER BY cr.started_at DESC, cr.rowid DESC LIMIT 1",
                (value, value, value, value),
            ).fetchone()
        return row["run_id"] if row else None

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
        reference = self._connection.execute(
            "SELECT started_at, rowid FROM collection_runs WHERE run_id = ?",
            (before_run_id,),
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
        precedes = "(cr.started_at < ? OR (cr.started_at = ? AND cr.rowid < ?))"
        if product_type is None:
            return self._connection.execute(
                f"SELECT run_id, started_at FROM collection_runs cr WHERE {precedes} "
                "ORDER BY cr.started_at DESC, cr.rowid DESC LIMIT 1",
                (reference_started_at, reference_started_at, reference_rowid),
            ).fetchone()
        # Same stored-value-or-EXISTS matching as latest_run_id(), for the
        # same reason: a legacy run that mixed both products under one
        # run_id (before record_observations() rejected that) must stay
        # findable under either product, not just whichever one the
        # migration's single-column backfill happened to pick -- all
        # three signals _migrate_collection_runs_product_type() backfills
        # from, including a mixed bootstrap run's only surviving trace,
        # first_seen_run_id (correlated by actual run_id, like
        # last_seen_run_id/rate_changes.run_id above -- not by matching
        # first_observed_at against started_at, which would also match
        # an unrelated run captured at the same timestamp).
        value = _product_value(product_type)
        return self._connection.execute(
            f"SELECT cr.run_id, cr.started_at FROM collection_runs cr WHERE {precedes} "
            "AND (cr.product_type = ? "
            "     OR EXISTS (SELECT 1 FROM observations o "
            "                WHERE o.last_seen_run_id = cr.run_id AND o.product_type = ?) "
            "     OR EXISTS (SELECT 1 FROM rate_changes rc "
            "                WHERE rc.run_id = cr.run_id AND rc.product_type = ?) "
            "     OR EXISTS (SELECT 1 FROM observations o2 "
            "                WHERE o2.first_seen_run_id = cr.run_id AND o2.product_type = ?)) "
            "ORDER BY cr.started_at DESC, cr.rowid DESC LIMIT 1",
            (reference_started_at, reference_started_at, reference_rowid, value, value, value, value),
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
        """
        previous = self._previous_run_row(before_run_id, product_type)
        return previous["run_id"] if previous is not None else None

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
