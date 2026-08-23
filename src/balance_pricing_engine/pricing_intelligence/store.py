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
from typing import Iterable, Iterator, Optional, Sequence
import uuid

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.changes import ChangeType

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id              TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL,
    banks               TEXT NOT NULL,
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

# Excludes row `o` when another key `o3` in the same eligibility group (same
# bank/term/category/frequency/product_name/balance tier, i.e. the same
# product under a different eligibility condition -- see
# CompetitorRateRecord.eligibility_group_key()) first appeared after `o` was
# last confirmed: that is the signature of an eligibility-edit transition,
# where change detection deliberately leaves the old key's row untouched
# (see changes.resolve_pairing()) rather than closing it out. `{extra}` lets
# a caller reconstructing a historical point in time (get_observations_as_of)
# additionally bound `o3` to have existed by that point too -- an eligibility
# edit that happens after the reconstruction point does not apply.
_SUPERSEDED_BY_LATER_ELIGIBILITY_KEY = (
    "AND NOT EXISTS ("
    "  SELECT 1 FROM observations o3 WHERE o3.observation_key <> o.observation_key "
    "  AND o3.bank = o.bank AND o3.product_type = o.product_type "
    "  AND o3.rate_category = o.rate_category AND " + _SAME_TERM_IDENTITY + " "
    "  AND o3.interest_payment_frequency = o.interest_payment_frequency "
    "  AND LOWER(o3.product_name) = LOWER(o.product_name) "
    "  AND o3.min_balance IS o.min_balance AND o3.max_balance IS o.max_balance "
    "  AND o3.first_observed_at > o.last_seen_at{extra}"
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
        with self._connection:
            yield self._connection

    def _create_schema(self) -> None:
        with self._transaction() as connection:
            connection.executescript(_SCHEMA)
            row = connection.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
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
        """
        records = list(records)
        observed_at = observed_at or datetime.now(timezone.utc)
        run_id = run_id or str(uuid.uuid4())
        timestamp = _iso(observed_at)

        inserted = 0
        unchanged = 0
        banks = sorted({record.bank for record in records})

        with self._transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO collection_runs "
                "(run_id, started_at, banks, record_count, inserted_count, unchanged_count, "
                "report_json) VALUES (?, ?, ?, ?, 0, 0, ?)",
                (run_id, timestamp, json.dumps(banks), len(records), report_json),
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
        returned_after_a_withdrawal = current is not None and connection.execute(
            "SELECT 1 FROM rate_changes WHERE observation_key = ? "
            "AND change_type = 'REMOVED_PRODUCT' AND detected_at > ? LIMIT 1",
            (payload["observation_key"], current["last_seen_at"]),
        ).fetchone() is not None

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
        returned_after_an_eligibility_transition = current is not None and connection.execute(
            "SELECT 1 FROM observations WHERE observation_key <> ? "
            "AND bank = ? AND product_type = ? AND rate_category = ? "
            "AND ("
            "  (term_months IS NOT NULL AND ? IS NOT NULL AND term_months = ?)"
            "  OR (term_months IS NULL AND ? IS NULL AND LOWER(term_label_raw) = LOWER(?))"
            ") "
            "AND interest_payment_frequency = ? AND LOWER(product_name) = LOWER(?) "
            "AND min_balance IS ? AND max_balance IS ? AND first_observed_at > ? LIMIT 1",
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
                current["last_seen_at"],
            ),
        ).fetchone() is not None

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
            connection.execute(
                "UPDATE observations SET last_seen_at = ?, last_seen_run_id = ?, "
                "observation_count = observation_count + 1 WHERE id = ?",
                (timestamp, run_id, current["id"]),
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

    def confirm_suspicious_changes(self, observation_keys: Iterable[str]) -> None:
        """Clear the suspicious flag on the most recent change event for each key.

        A suspicious-but-accepted movement stays permanently excluded from
        market activity / market-state (both default to skipping suspicious
        changes) even after the same value repeats on a later, clean
        collection -- strong evidence it was a genuine reprice, not a parser
        fault. Called once ``_upsert_observation``'s own confirmation check
        (a repeated value no longer flagged this run) identifies the key.
        """
        with self._transaction() as connection:
            for key in observation_keys:
                connection.execute(
                    "UPDATE rate_changes SET suspicious = 0 WHERE id = ("
                    "  SELECT id FROM rate_changes WHERE observation_key = ? "
                    "  AND suspicious = 1 ORDER BY detected_at DESC, id DESC LIMIT 1"
                    ")",
                    (key,),
                )

    # ----------------------------------------------------------------- reads

    def latest_run_id(self) -> Optional[str]:
        row = self._connection.execute(
            "SELECT run_id FROM collection_runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
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
        run_id = run_id or self.latest_run_id()
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
            "  AND rc.change_type = 'REMOVED_PRODUCT' AND rc.detected_at > o.last_seen_at"
            ") " + _SUPERSEDED_BY_LATER_ELIGIBILITY_KEY.format(extra="") + "ORDER BY bank, term_months",
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
            "  AND rc.change_type = 'REMOVED_PRODUCT' AND rc.detected_at > o.last_seen_at"
            ") "
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

    def get_observations_as_of(
        self,
        as_of: datetime,
        product_type: ProductType | str = ProductType.HOME_LOAN,
    ) -> list[CompetitorRateRecord]:
        """Reconstruct the advertised state of every key as of a given time.

        ``last_seen_run_id`` is unsuitable for this: it is overwritten forward
        on every unchanged re-observation, so it cannot say what a key looked
        like at an earlier run once a later run has touched it. This instead
        picks, per key, the most recent row whose ``first_observed_at`` is at
        or before ``as_of`` -- the state that was actually current then.

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
        rows = self._connection.execute(
            "SELECT * FROM observations o WHERE product_type = ? AND first_observed_at <= ? "
            "AND id = ("
            "  SELECT id FROM observations o2 WHERE o2.observation_key = o.observation_key "
            "  AND o2.first_observed_at <= ? ORDER BY first_observed_at DESC, id DESC LIMIT 1"
            ") "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM rate_changes rc WHERE rc.observation_key = o.observation_key "
            "  AND rc.change_type = 'REMOVED_PRODUCT' AND rc.detected_at <= ? "
            "  AND rc.detected_at > o.first_observed_at"
            ") "
            + _SUPERSEDED_BY_LATER_ELIGIBILITY_KEY.format(extra=" AND o3.first_observed_at <= ?")
            + "ORDER BY bank, term_months",
            (_product_value(product_type), timestamp, timestamp, timestamp, timestamp),
        ).fetchall()
        return [_row_to_history_record(row) for row in rows]

    def get_confirmed_observations_as_of(
        self,
        as_of: datetime,
        product_type: ProductType | str = ProductType.HOME_LOAN,
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
        rows = self._connection.execute(
            "SELECT * FROM observations o WHERE product_type = ? AND first_observed_at <= ? "
            "AND validation_status != 'warning' "
            "AND id = ("
            "  SELECT id FROM observations o2 WHERE o2.observation_key = o.observation_key "
            "  AND o2.first_observed_at <= ? AND o2.validation_status != 'warning' "
            "  ORDER BY first_observed_at DESC, id DESC LIMIT 1"
            ") "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM rate_changes rc WHERE rc.observation_key = o.observation_key "
            "  AND rc.change_type = 'REMOVED_PRODUCT' AND rc.detected_at <= ? "
            "  AND rc.detected_at > o.first_observed_at"
            ") "
            + _SUPERSEDED_BY_LATER_ELIGIBILITY_KEY.format(
                extra=" AND o3.first_observed_at <= ? AND o3.validation_status != 'warning'"
            )
            + "ORDER BY bank, term_months",
            (_product_value(product_type), timestamp, timestamp, timestamp, timestamp),
        ).fetchall()
        return [_row_to_history_record(row) for row in rows]

    def get_previous_run_started_at(self, before_run_id: Optional[str] = None) -> Optional[datetime]:
        """Resolve the start time of the run before ``before_run_id`` (default: the latest run).

        Exposed separately from :meth:`get_previous_run_rates` so a caller can
        also pass this timestamp on as the snapshot's ``as_of``, rather than
        letting it be inferred from the reconstructed records themselves.
        """
        before_run_id = before_run_id or self.latest_run_id()
        if before_run_id is None:
            return None
        started_at = self.get_run_started_at(before_run_id)
        if started_at is None:
            return None
        previous = self._connection.execute(
            "SELECT started_at FROM collection_runs WHERE started_at < ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (_iso(started_at),),
        ).fetchone()
        return _parse_iso(previous["started_at"]) if previous is not None else None

    def get_previous_run_rates(
        self,
        product_type: ProductType | str = ProductType.HOME_LOAN,
        before_run_id: Optional[str] = None,
    ) -> list[CompetitorRateRecord]:
        """Return the advertised rates from the run before ``before_run_id``."""
        as_of = self.get_previous_run_started_at(before_run_id)
        if as_of is None:
            return []
        return self.get_observations_as_of(as_of=as_of, product_type=product_type)

    def get_changes_since(
        self,
        since: Optional[datetime] = None,
        days: Optional[int] = None,
        bank: Optional[str] = None,
        term_months: Optional[int] = None,
        include_suspicious: bool = True,
        now: Optional[datetime] = None,
    ) -> list[dict]:
        """Return stored change events in a window, newest first.

        Returns plain dicts; ``changes.load_changes`` rehydrates them into
        ``RateChange`` objects for callers that want the typed form.
        """
        now = now or datetime.now(timezone.utc)
        if since is None and days is not None:
            since = now - timedelta(days=days)
        clauses: list[str] = []
        params: list[object] = []
        if since is not None:
            clauses.append("detected_at >= ?")
            params.append(_iso(since))
        if bank is not None:
            clauses.append("bank = ?")
            params.append(bank)
        if term_months is not None:
            clauses.append("term_months = ?")
            params.append(term_months)
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


def _row_to_history_record(row: sqlite3.Row) -> CompetitorRateRecord:
    """Like :func:`_row_to_record`, but dates the state from when it began.

    History must reflect how long a state actually held, not when it was
    last reconfirmed: an unchanged state's ``last_seen_at`` creeps forward on
    every identical collection, which would otherwise make the dashboard lose
    a state's true start date.
    """
    return _row_to_record(row).model_copy(
        update={"captured_at_utc": record_first_observed_at(row)}
    )
