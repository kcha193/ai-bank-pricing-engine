"""Persistence and state machine for the quarantine review ledger (DATA-002).

Functions take the :class:`~.store.ObservationStore` they operate on and are exposed
through thin methods on it, so ``store.py`` stays the single owner of the connection and
transaction conventions. See :mod:`.quarantine` for the trust model.

Guarantees
----------
* **Append-only.** ``quarantine_reviews`` rejects ``UPDATE``/``DELETE`` via triggers.
* **Exact targets.** A decision names one ``rate_changes.id``; it is never "the latest
  suspicious event". The event is mapped to the single observation row it introduced.
* **Transactional, race-safe.** The state is read and the decision written under one
  ``BEGIN IMMEDIATE`` write lock, and ``UNIQUE (observation_id, previous_state)`` makes a
  second contradictory decision from the same prior state impossible even without it.
* **Idempotent.** Repeating a decision that already stands returns the existing ledger row.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Optional

from balance_pricing_engine.pricing_intelligence.changes import resolve_pairing
from balance_pricing_engine.pricing_intelligence.quarantine import (
    ALLOWED_FROM,
    RESULT_STATE,
    UNTRUSTING_STATES,
    InvalidReviewTransition,
    QuarantineEscalation,
    QuarantineStatus,
    ReviewAction,
    ReviewCase,
    ReviewEvent,
    ReviewInputError,
    ReviewOutcome,
    ReviewState,
    ReviewTargetNotFound,
    validate_review_input,
)

if TYPE_CHECKING:  # store.py imports this module's callers; avoid a runtime cycle
    from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord
    from balance_pricing_engine.pricing_intelligence.store import ObservationStore

#: Rate-movement events are the reviewable unit; companion eligibility/promotion events
#: share the target's observation key and run and follow its decision.
RATE_EVENT_TYPES = ("RATE_INCREASE", "RATE_DECREASE")

REVIEW_SCHEMA = """
-- Append-only ledger of operator decisions on quarantined or previously trusted
-- readings (DATA-002). A row is never updated or deleted: the audit trail of who
-- decided what, when, why and on what evidence is the whole point. Observation
-- rows are never rewritten either; effective trust is observations.validation_status
-- overlaid with the latest decision here (see store._effective_observations()).
CREATE TABLE IF NOT EXISTS quarantine_reviews (
    review_seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id          TEXT NOT NULL UNIQUE,
    action             TEXT NOT NULL CHECK (action IN ('APPROVE', 'REJECT', 'VOID')),
    observation_id     INTEGER NOT NULL REFERENCES observations (id),
    change_id          INTEGER,
    observation_key    TEXT NOT NULL,
    bank               TEXT NOT NULL,
    product_type       TEXT NOT NULL,
    term_months        INTEGER,
    rate_category      TEXT NOT NULL,
    run_id             TEXT NOT NULL,
    previous_rate_pct  REAL,
    proposed_rate_pct  REAL,
    reviewer           TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
    reviewed_at        TEXT NOT NULL,
    reason             TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    evidence_ref       TEXT,
    previous_state     TEXT NOT NULL,
    new_state          TEXT NOT NULL CHECK (new_state IN ('APPROVED', 'REJECTED', 'VOIDED')),
    -- A second decision from the same prior state cannot exist: the first one moved it on.
    UNIQUE (observation_id, previous_state)
);

CREATE INDEX IF NOT EXISTS idx_quarantine_reviews_observation
    ON quarantine_reviews (observation_id, review_seq);
CREATE INDEX IF NOT EXISTS idx_quarantine_reviews_change
    ON quarantine_reviews (change_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_reviews_void
    ON quarantine_reviews (observation_key, run_id, product_type) WHERE new_state = 'VOIDED';

-- Delivery ledger for quarantine escalation / resolution notices, one row per quarantined
-- observation and channel, so an unresolved reading alerts once -- not on every run or retry --
-- and its resolution is announced once. Only confirmed deliveries are recorded: a failed send
-- stays eligible for the next run (the convention notified_alerts and the collector-health
-- ledger already follow).
CREATE TABLE IF NOT EXISTS quarantine_escalation_alerts (
    observation_id    INTEGER NOT NULL REFERENCES observations (id),
    channel           TEXT NOT NULL,
    product_type      TEXT NOT NULL,
    bank              TEXT NOT NULL,
    term_months       INTEGER,
    sent_at           TEXT NOT NULL,
    resolved_sent_at  TEXT,
    PRIMARY KEY (observation_id, channel)
);

CREATE TRIGGER IF NOT EXISTS quarantine_reviews_no_update
BEFORE UPDATE ON quarantine_reviews
BEGIN SELECT RAISE(ABORT, 'quarantine_reviews is append-only'); END;

CREATE TRIGGER IF NOT EXISTS quarantine_reviews_no_delete
BEFORE DELETE ON quarantine_reviews
BEGIN SELECT RAISE(ABORT, 'quarantine_reviews is append-only'); END;
"""


def _s():
    """The store module (imported lazily: it imports this module)."""
    from balance_pricing_engine.pricing_intelligence import store

    return store


def _utc(value: Optional[datetime]) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def latest_review_state(
    connection: sqlite3.Connection, observation_id: int
) -> Optional[ReviewState]:
    row = connection.execute(
        "SELECT new_state FROM quarantine_reviews WHERE observation_id = ? "
        "ORDER BY review_seq DESC LIMIT 1",
        (observation_id,),
    ).fetchone()
    return ReviewState(row["new_state"]) if row else None


def _event_from_row(row: sqlite3.Row) -> ReviewEvent:
    return ReviewEvent(
        review_id=row["review_id"],
        review_seq=row["review_seq"],
        action=ReviewAction(row["action"]),
        observation_id=row["observation_id"],
        change_id=row["change_id"],
        observation_key=row["observation_key"],
        bank=row["bank"],
        product_type=row["product_type"],
        term_months=row["term_months"],
        rate_category=row["rate_category"],
        run_id=row["run_id"],
        previous_rate_pct=row["previous_rate_pct"],
        proposed_rate_pct=row["proposed_rate_pct"],
        reviewer=row["reviewer"],
        reviewed_at=_s()._parse_iso(row["reviewed_at"]),
        reason=row["reason"],
        evidence_ref=row["evidence_ref"],
        previous_state=ReviewState(row["previous_state"]),
        new_state=ReviewState(row["new_state"]),
    )


def _observation_for_change(
    connection: sqlite3.Connection, change: sqlite3.Row
) -> Optional[sqlite3.Row]:
    """The one observation row a rate event introduced (same key, product and run)."""
    return connection.execute(
        "SELECT * FROM observations WHERE observation_key = ? AND product_type = ? "
        "AND first_seen_run_id = ? ORDER BY id DESC LIMIT 1",
        (change["observation_key"], change["product_type"], change["run_id"]),
    ).fetchone()


def _pending_chain(
    connection: sqlite3.Connection, observation_key: str, product_type: str
) -> tuple[int, Optional[str]]:
    """Distinct runs and start time of the key's current unreviewed quarantine.

    ``observation_count`` advances once per run that observed the stored state (a replay of
    the same run_id is an inert no-op), so summing it over the contiguous trailing
    quarantined-and-unreviewed rows counts distinct runs for this reading identity.
    """
    rows = connection.execute(
        "SELECT id, validation_status, observation_count, first_observed_at FROM observations "
        "WHERE observation_key = ? AND product_type = ? "
        "ORDER BY first_observed_at DESC, id DESC",
        (observation_key, product_type),
    ).fetchall()
    runs, since = 0, None
    for row in rows:
        if row["validation_status"] != "warning" or latest_review_state(connection, row["id"]):
            break
        runs += row["observation_count"]
        since = row["first_observed_at"]
    return runs, since


def _build_case(
    connection: sqlite3.Connection, change: sqlite3.Row, now: datetime
) -> Optional[ReviewCase]:
    observation = _observation_for_change(connection, change)
    if observation is None:
        return None
    state = latest_review_state(connection, observation["id"])
    runs_pending, since = 0, None
    if state is None:
        if observation["validation_status"] == "warning":
            newest = connection.execute(
                "SELECT id FROM observations WHERE observation_key = ? AND product_type = ? "
                "ORDER BY first_observed_at DESC, id DESC LIMIT 1",
                (observation["observation_key"], observation["product_type"]),
            ).fetchone()
            if newest["id"] == observation["id"]:
                state = ReviewState.PENDING
                runs_pending, since = _pending_chain(
                    connection, observation["observation_key"], observation["product_type"]
                )
            else:
                state = ReviewState.SUPERSEDED
        else:
            state = ReviewState.ACCEPTED
    started = _s()._parse_iso(since) if since else _s()._parse_iso(change["detected_at"])
    age_hours = max(0.0, (now - started).total_seconds() / 3600)
    try:
        signals = tuple(str(item) for item in json.loads(observation["validation_warnings"]))
    except (TypeError, ValueError):
        signals = ()
    return ReviewCase(
        event_id=change["id"],
        observation_id=observation["id"],
        observation_key=change["observation_key"],
        bank=change["bank"],
        product_type=change["product_type"],
        term_months=change["term_months"],
        rate_category=change["rate_category"],
        run_id=change["run_id"],
        previous_rate_pct=change["previous_rate_pct"],
        proposed_rate_pct=change["current_rate_pct"],
        change_bps=change["change_bps"],
        detected_at=_s()._parse_iso(change["detected_at"]),
        status=state,
        runs_pending=runs_pending,
        age_hours=age_hours,
        source_url=change["source_url"] or observation["source_url"],
        evidence_text=observation["source_text"],
        signals=signals,
    )


def _change_rows(
    connection: sqlite3.Connection,
    *,
    product_type: Optional[str] = None,
    event_id: Optional[int] = None,
    include_unflagged: bool = True,
) -> list[sqlite3.Row]:
    clauses = [f"change_type IN ({', '.join('?' * len(RATE_EVENT_TYPES))})"]
    params: list[object] = list(RATE_EVENT_TYPES)
    if product_type is not None:
        clauses.append("product_type = ?")
        params.append(product_type)
    if event_id is not None:
        clauses.append("id = ?")
        params.append(event_id)
    if not include_unflagged:
        # Only events that are (or were) in quarantine: still flagged, or already decided.
        clauses.append(
            "(suspicious = 1 OR EXISTS (SELECT 1 FROM quarantine_reviews r "
            "WHERE r.change_id = rate_changes.id))"
        )
    return connection.execute(
        f"SELECT * FROM rate_changes WHERE {' AND '.join(clauses)} ORDER BY id", params
    ).fetchall()


def list_review_cases(
    store: "ObservationStore",
    product_type,
    statuses: Optional[Iterable[ReviewState]] = None,
    now: Optional[datetime] = None,
) -> list[ReviewCase]:
    """Review cases for a product, oldest first; PENDING only unless ``statuses`` says otherwise."""
    wanted = frozenset(statuses) if statuses is not None else frozenset({ReviewState.PENDING})
    now = _utc(now)
    connection = store._connection
    rows = _change_rows(
        connection,
        product_type=_s()._product_value(product_type),
        include_unflagged=ReviewState.ACCEPTED in wanted,
    )
    cases = (_build_case(connection, row, now) for row in rows)
    return [case for case in cases if case is not None and case.status in wanted]


def get_review_case(
    store: "ObservationStore", event_id: int, now: Optional[datetime] = None
) -> ReviewCase:
    connection = store._connection
    rows = _change_rows(connection, event_id=event_id)
    case = _build_case(connection, rows[0], _utc(now)) if rows else None
    if case is None:
        raise ReviewTargetNotFound(f"event {event_id} is not a reviewable rate-change event")
    return case


def get_review_history(store: "ObservationStore", event_id: int) -> list[ReviewEvent]:
    case = get_review_case(store, event_id)
    rows = store._connection.execute(
        "SELECT * FROM quarantine_reviews WHERE observation_id = ? ORDER BY review_seq",
        (case.observation_id,),
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def _check_transition(case: ReviewCase, action: ReviewAction) -> None:
    if case.status not in ALLOWED_FROM[action]:
        allowed = ", ".join(sorted(state.value for state in ALLOWED_FROM[action]))
        raise InvalidReviewTransition(
            f"cannot {action.value} event {case.event_id}: it is {case.status.value} "
            f"({action.value} applies to {allowed})"
        )


def _trusted_predecessor(
    connection: sqlite3.Connection, observation: sqlite3.Row,
    review_cutoff: Optional[str] = None,
) -> Optional["CompetitorRateRecord"]:
    effective = _s()._effective_observations(review_cutoff)
    # Use the same trusted eligibility-supersession rule as observation fallbacks.
    # Exact-key lookup alone misses a reprice that also changes eligibility.
    rows = connection.execute(
        f"SELECT * FROM {effective} o WHERE o.bank = ? AND o.product_type = ? "
        "AND o.id < ? AND o.eff_status NOT IN ('warning', 'voided') AND o.id = ("
        f"SELECT o2.id FROM {effective} o2 WHERE o2.observation_key = o.observation_key "
        "AND o2.id < ? AND o2.eff_status NOT IN ('warning', 'voided') "
        "ORDER BY o2.first_observed_at DESC, o2.id DESC LIMIT 1) "
        + _s()._SUPERSEDED_BY_LATER_ELIGIBILITY_KEY.format(
            extra=" AND o3.id < ? AND o3.eff_status NOT IN ('warning', 'voided')",
            o3_src=effective,
        ),
        (observation["bank"], observation["product_type"], observation["id"],
         observation["id"], observation["id"]),
    ).fetchall()
    record = _s()._row_to_record(observation)
    return resolve_pairing([_s()._row_to_record(row) for row in rows], [record]).get(
        record.observation_key()
    )


def apply_review(
    store: "ObservationStore",
    event_id: int,
    action: ReviewAction,
    *,
    reviewer: Optional[str],
    reason: Optional[str],
    evidence_ref: Optional[str] = None,
    reviewed_at: Optional[datetime] = None,
    dry_run: bool = False,
) -> ReviewOutcome:
    """Record one decision on one rate-change event. See the module docstring."""
    action = ReviewAction(action)
    reviewer, reason, evidence_ref = validate_review_input(
        action, reviewer=reviewer, reason=reason, evidence_ref=evidence_ref
    )
    when = _utc(reviewed_at)
    if dry_run:
        case = get_review_case(store, event_id, now=when)
        if case.status is not RESULT_STATE[action]:
            _check_transition(case, action)
        return ReviewOutcome(case=case, event=None, created=False, dry_run=True)

    with store._write_transaction() as connection:
        rows = _change_rows(connection, event_id=event_id)
        case = _build_case(connection, rows[0], when) if rows else None
        if case is None:
            raise ReviewTargetNotFound(f"event {event_id} is not a reviewable rate-change event")
        if case.status is RESULT_STATE[action]:
            existing = connection.execute(
                "SELECT * FROM quarantine_reviews WHERE observation_id = ? AND new_state = ? "
                "ORDER BY review_seq DESC LIMIT 1",
                (case.observation_id, case.status.value),
            ).fetchone()
            return ReviewOutcome(case=case, event=_event_from_row(existing), created=False)
        _check_transition(case, action)
        last = connection.execute(
            "SELECT MAX(reviewed_at) FROM quarantine_reviews WHERE observation_id = ?",
            (case.observation_id,),
        ).fetchone()[0]
        floor = max(filter(None, (_s()._iso(case.detected_at), last)))
        if _s()._iso(when) < floor:
            raise ReviewInputError(
                f"reviewed_at {_s()._iso(when)} precedes the event's own timeline ({floor})"
            )
        new_state = RESULT_STATE[action]
        review_id = uuid.uuid4().hex
        try:
            connection.execute(
                "INSERT INTO quarantine_reviews (review_id, action, observation_id, change_id, "
                "observation_key, bank, product_type, term_months, rate_category, run_id, "
                "previous_rate_pct, proposed_rate_pct, reviewer, reviewed_at, reason, "
                "evidence_ref, previous_state, new_state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    action.value,
                    case.observation_id,
                    case.event_id,
                    case.observation_key,
                    case.bank,
                    case.product_type,
                    case.term_months,
                    case.rate_category,
                    case.run_id,
                    case.previous_rate_pct,
                    case.proposed_rate_pct,
                    reviewer,
                    _s()._iso(when),
                    reason,
                    evidence_ref,
                    case.status.value,
                    new_state.value,
                ),
            )
        except sqlite3.IntegrityError as exc:  # a contradicting decision won the race
            raise InvalidReviewTransition(
                f"event {event_id} was decided concurrently; re-read its state"
            ) from exc
        event = _event_from_row(
            connection.execute(
                "SELECT * FROM quarantine_reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
        )
        refreshed = _build_case(connection, rows[0], when)
        assert refreshed is not None  # the observation exists: it was just resolved above
        return ReviewOutcome(case=refreshed, event=event, created=True)


def _original_predecessor_time(
    store: "ObservationStore", observation: sqlite3.Row
) -> Optional[str]:
    """Recover a legacy-rebased event's original timestamp, including eligibility edits."""
    record = _s()._row_to_record(observation)
    previous = resolve_pairing(
        store.get_previous_run_rates(
            product_type=observation["product_type"], before_run_id=observation["first_seen_run_id"]
        ), [record]
    ).get(record.observation_key())
    if previous is None:
        return None
    row = store._connection.execute(
        "SELECT last_seen_at FROM observations WHERE observation_key = ? AND id < ? "
        "ORDER BY first_observed_at DESC, id DESC LIMIT 1",
        (previous.observation_key(), observation["id"]),
    ).fetchone()
    return row["last_seen_at"] if row is not None else None


def effective_changes(
    store: "ObservationStore", rows: Iterable[sqlite3.Row], now: datetime,
    *, include_voided: bool = False,
) -> list[dict]:
    """Project event trust at ``now`` without rewriting collected evidence.

    Exact event decisions also govern same-run companions. Approval rebases only the
    rate event; companions describing an intermediate, untrusted hop stay suspicious.
    The observation's immutable warning also recovers original suspicion for databases
    where the earlier approval implementation already cleared the stored event flag.
    """
    connection = store._connection
    cutoff = _s()._iso(now)
    result = []
    for row in rows:
        change = dict(row)
        reviews = connection.execute(
            "SELECT * FROM quarantine_reviews WHERE observation_key = ? AND run_id = ? "
            "AND product_type = ? ORDER BY review_seq",
            (row["observation_key"], row["run_id"], row["product_type"]),
        ).fetchall()
        if not reviews:
            result.append(change)
            continue
        observation = connection.execute(
            "SELECT * FROM observations WHERE id = ?", (reviews[0]["observation_id"],)
        ).fetchone()
        if observation["validation_status"] == "warning":
            change["suspicious"] = 1
        decisions = [r for r in reviews if r["reviewed_at"] <= cutoff]
        if decisions and decisions[-1]["new_state"] == "VOIDED" and not include_voided:
            continue
        approval = next((r for r in decisions if r["new_state"] == "APPROVED"), None)
        if approval is not None:
            predecessor = _trusted_predecessor(connection, observation, approval["reviewed_at"])
            rebasing = (
                predecessor is not None
                and predecessor.advertised_rate_pct != approval["previous_rate_pct"]
            )
            if row["id"] == approval["change_id"]:
                change["suspicious"] = 0
                if predecessor is not None:
                    change["previous_rate_pct"] = predecessor.advertised_rate_pct
                    change["current_rate_pct"] = observation["rate_pct"]
                    change["change_bps"] = _s().rate_change_bps(
                        predecessor.advertised_rate_pct, observation["rate_pct"]
                    )
                    change["previous_observed_at"] = _s()._iso(predecessor.captured_at_utc)
                    change["change_type"] = (
                        "RATE_INCREASE" if change["change_bps"] > 0 else "RATE_DECREASE"
                    )
            elif not rebasing:
                change["suspicious"] = 0
        elif observation["validation_status"] == "warning" and row["id"] == reviews[0]["change_id"]:
            # Legacy approvals may also have rebased the mutable event. Recover its
            # original previous endpoint from the ledger for pre-approval audit reads.
            previous = reviews[0]["previous_rate_pct"]
            current = reviews[0]["proposed_rate_pct"]
            change["previous_rate_pct"] = previous
            change["current_rate_pct"] = current
            change["change_bps"] = _s().rate_change_bps(previous, current)
            change["change_type"] = "RATE_INCREASE" if change["change_bps"] > 0 else "RATE_DECREASE"
            if not row["suspicious"]:
                change["previous_observed_at"] = _original_predecessor_time(store, observation)
        result.append(change)
    return result


def pending_status(
    store: "ObservationStore",
    product_type,
    escalation_after_runs: int,
    now: Optional[datetime] = None,
) -> QuarantineStatus:
    pending = tuple(list_review_cases(store, product_type, now=now))
    escalated = tuple(
        QuarantineEscalation(
            event_id=case.event_id,
            observation_id=case.observation_id,
            bank=case.bank,
            product_type=case.product_type,
            term_months=case.term_months,
            rate_category=case.rate_category,
            observation_key=case.observation_key,
            runs_pending=case.runs_pending,
            age_hours=case.age_hours,
            previous_rate_pct=case.previous_rate_pct,
            proposed_rate_pct=case.proposed_rate_pct,
        )
        for case in pending
        if case.runs_pending >= escalation_after_runs
    )
    return QuarantineStatus(
        pending=pending, escalated=escalated, escalation_after_runs=escalation_after_runs
    )


def repeats_reviewed_untrusted(
    store: "ObservationStore", record: "CompetitorRateRecord", *, include_pending: bool = False
) -> bool:
    """True when ``record`` is the same reading an operator already rejected or voided.

    Such a reading must not re-enter the trusted market merely because the same source text
    keeps appearing; a *different* reading is judged afresh. ``include_pending`` extends
    that protection to unreviewed warnings when a new peer check abstains or passes.
    """
    module = _s()
    connection = store._connection
    row = connection.execute(
        "SELECT * FROM observations WHERE observation_key = ? ORDER BY first_observed_at DESC, "
        "id DESC LIMIT 1",
        (record.observation_key(),),
    ).fetchone()
    if row is None:
        return False
    state = latest_review_state(connection, row["id"])
    untrusted = state in UNTRUSTING_STATES or (
        include_pending and state is None and row["validation_status"] == "warning"
    )
    if not untrusted:
        return False
    return module._is_unchanged(row, module._record_to_row(record, "", ""))


def decided_untrusted_keys(store: "ObservationStore", product_type) -> set[str]:
    """Identities whose *current* reading an operator rejected or voided.

    Such a reading is untrusted but no longer an open quarantine: health must not keep
    reporting the bank as quarantined once the operator has decided.
    """
    rows = store._connection.execute(
        "SELECT o.id, o.observation_key FROM observations o WHERE o.product_type = ? AND o.id = ("
        "  SELECT id FROM observations o2 WHERE o2.observation_key = o.observation_key "
        "  ORDER BY first_observed_at DESC, id DESC LIMIT 1)",
        (_s()._product_value(product_type),),
    ).fetchall()
    return {
        row["observation_key"]
        for row in rows
        if latest_review_state(store._connection, row["id"]) in UNTRUSTING_STATES
    }


def unalerted_escalations(
    store: "ObservationStore", status: QuarantineStatus, channel: str
) -> list[QuarantineEscalation]:
    """Escalated quarantines this channel has not yet been told about."""
    return [
        escalation
        for escalation in status.escalated
        if store._connection.execute(
            "SELECT 1 FROM quarantine_escalation_alerts WHERE observation_id = ? AND channel = ?",
            (escalation.observation_id, channel),
        ).fetchone()
        is None
    ]


def record_escalation_alerts(
    store: "ObservationStore",
    escalations: Iterable[QuarantineEscalation],
    channel: str,
    sent_at: datetime,
) -> None:
    with store._transaction() as connection:
        for escalation in escalations:
            connection.execute(
                "INSERT OR IGNORE INTO quarantine_escalation_alerts (observation_id, channel, "
                "product_type, bank, term_months, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    escalation.observation_id,
                    channel,
                    escalation.product_type,
                    escalation.bank,
                    escalation.term_months,
                    _s()._iso(sent_at),
                ),
            )


def unannounced_resolutions(
    store: "ObservationStore", product_type, status: QuarantineStatus, channel: str
) -> list[sqlite3.Row]:
    """Quarantines this channel was alerted about that are no longer pending."""
    still_pending = {case.observation_id for case in status.pending}
    rows = store._connection.execute(
        "SELECT * FROM quarantine_escalation_alerts WHERE product_type = ? AND channel = ? "
        "AND resolved_sent_at IS NULL ORDER BY observation_id",
        (_s()._product_value(product_type), channel),
    ).fetchall()
    return [row for row in rows if row["observation_id"] not in still_pending]


def record_resolution_alerts(
    store: "ObservationStore", rows: Iterable[sqlite3.Row], channel: str, sent_at: datetime
) -> None:
    with store._transaction() as connection:
        for row in rows:
            connection.execute(
                "UPDATE quarantine_escalation_alerts SET resolved_sent_at = ? "
                "WHERE observation_id = ? AND channel = ? AND resolved_sent_at IS NULL",
                (_s()._iso(sent_at), row["observation_id"], channel),
            )
