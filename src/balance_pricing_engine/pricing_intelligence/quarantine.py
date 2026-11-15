"""Operator review of quarantined competitor readings (DATA-002).

A reading is *quarantined* when validation flags it (a movement over
``suspicious_change_bps``, a peer-market outlier, or a repeat of a reading an
operator already refused). It is stored for audit but never enters a trusted market
view. Until now nothing could release one: a genuine +100 bps reprice stayed
quarantined until the bank dropped out of the market.

This module holds the vocabulary of the governed workflow; the persistence and the
transactional state machine live in :mod:`.quarantine_store`.

Trust model
-----------
Every decision is a row in the append-only ``quarantine_reviews`` ledger. Observation
rows are never rewritten: the *effective* trust of a stored reading is its raw
validation status overlaid with the latest ledger decision, as of the time the view is
asked for. So a historical (as-of) view never shows a reading as approved before the
approval happened, and a void takes effect from the time it was made.

``REJECT`` and ``VOID`` differ deliberately:

* **REJECT** -- the reading was never trusted. It stays out of every trusted view.
* **VOID** -- the reading *was* trusted (accepted, or approved earlier) and is now
  withdrawn. Its influence is removed from current rankings, benchmarks, market state
  and change statistics; history before the void is preserved.

An approval confirms that the *reading matches independent evidence*. It says nothing
about whether the rate is financially attractive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Optional


class ReviewAction(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    VOID = "VOID"


class ReviewState(str, Enum):
    #: Quarantined, awaiting a decision.
    PENDING = "PENDING"
    #: Accepted by validation with no review (the only state a VOID may start from, besides APPROVED).
    ACCEPTED = "ACCEPTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    VOIDED = "VOIDED"
    #: Quarantined, but a newer reading of the same identity replaced it; nothing to decide.
    SUPERSEDED = "SUPERSEDED"


#: Resulting state of each action.
RESULT_STATE = {
    ReviewAction.APPROVE: ReviewState.APPROVED,
    ReviewAction.REJECT: ReviewState.REJECTED,
    ReviewAction.VOID: ReviewState.VOIDED,
}

#: States each action may start from. Anything else is an invalid transition.
ALLOWED_FROM = {
    ReviewAction.APPROVE: frozenset({ReviewState.PENDING}),
    ReviewAction.REJECT: frozenset({ReviewState.PENDING}),
    ReviewAction.VOID: frozenset({ReviewState.ACCEPTED, ReviewState.APPROVED}),
}

#: Ledger states that make a stored reading untrusted regardless of its raw status.
UNTRUSTING_STATES = frozenset({ReviewState.REJECTED, ReviewState.VOIDED})


class ReviewError(Exception):
    """Base class for review-workflow failures that the CLI reports without a traceback."""


class ReviewInputError(ReviewError, ValueError):
    """A required review field is missing or malformed."""


class ReviewTargetNotFound(ReviewError, LookupError):
    """The named event is not a reviewable rate-change event."""


class InvalidReviewTransition(ReviewError):
    """The event's current review state does not allow this action."""


@dataclass(frozen=True)
class ReviewEvent:
    """One immutable row of the review ledger."""

    review_id: str
    review_seq: int
    action: ReviewAction
    observation_id: int
    change_id: Optional[int]
    observation_key: str
    bank: str
    product_type: str
    term_months: Optional[int]
    rate_category: str
    run_id: str
    previous_rate_pct: Optional[float]
    proposed_rate_pct: Optional[float]
    reviewer: str
    reviewed_at: datetime
    reason: str
    evidence_ref: Optional[str]
    previous_state: ReviewState
    new_state: ReviewState


@dataclass(frozen=True)
class ReviewCase:
    """A rate-change event as the review queue shows it."""

    event_id: int
    observation_id: int
    observation_key: str
    bank: str
    product_type: str
    term_months: Optional[int]
    rate_category: str
    run_id: str
    previous_rate_pct: Optional[float]
    proposed_rate_pct: Optional[float]
    change_bps: Optional[float]
    detected_at: datetime
    status: ReviewState
    #: Distinct collection runs this reading identity has been quarantined and unreviewed.
    runs_pending: int
    #: Hours since detection (meaningful for PENDING).
    age_hours: float
    source_url: str
    #: The stored source text the reading was parsed from.
    evidence_text: str
    #: Validation findings recorded with the reading (e.g. ``peer_outlier: ...``).
    signals: tuple[str, ...]


@dataclass(frozen=True)
class ReviewOutcome:
    """Result of one review command.

    ``created`` is False for an idempotent repeat (the decision already stood) and for a
    dry run; ``event`` is the ledger row that stands for the decision (``None`` on a dry run).
    """

    case: ReviewCase
    event: Optional[ReviewEvent]
    created: bool
    dry_run: bool = False


@dataclass(frozen=True)
class QuarantineEscalation:
    """A pending quarantine that has been unresolved for at least the policy threshold."""

    event_id: int
    observation_id: int
    bank: str
    product_type: str
    term_months: Optional[int]
    rate_category: str
    observation_key: str
    runs_pending: int
    age_hours: float
    previous_rate_pct: Optional[float]
    proposed_rate_pct: Optional[float]


@dataclass(frozen=True)
class QuarantineStatus:
    """Pending quarantines for one product, for health, summaries and escalation."""

    pending: tuple[ReviewCase, ...]
    escalated: tuple[QuarantineEscalation, ...]
    escalation_after_runs: int

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    @property
    def oldest_age_hours(self) -> Optional[float]:
        return max((case.age_hours for case in self.pending), default=None)

    @property
    def affected_banks(self) -> tuple[str, ...]:
        return tuple(sorted({case.bank for case in self.pending}))


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
MAX_REASON_LENGTH = 1000
MAX_EVIDENCE_LENGTH = 500
MAX_REVIEWER_LENGTH = 100


def _clean(label: str, value: Optional[str], *, required: bool, limit: int) -> Optional[str]:
    text = (value or "").strip()
    if not text:
        if required:
            raise ReviewInputError(f"{label} is required")
        return None
    if len(text) > limit:
        raise ReviewInputError(f"{label} must be at most {limit} characters")
    if _CONTROL_CHARS.search(text):
        raise ReviewInputError(f"{label} must not contain control characters")
    return text


def validate_review_input(
    action: ReviewAction,
    *,
    reviewer: Optional[str],
    reason: Optional[str],
    evidence_ref: Optional[str],
) -> tuple[str, str, Optional[str]]:
    """Return the cleaned ``(reviewer, reason, evidence_ref)`` or raise ReviewInputError.

    An approval must cite independent evidence (a URL or an immutable reference such as a
    document hash or ticket id); a rejection or void may, but need not.
    """
    reviewer_clean = _clean("reviewer", reviewer, required=True, limit=MAX_REVIEWER_LENGTH)
    reason_clean = _clean("reason", reason, required=True, limit=MAX_REASON_LENGTH)
    evidence_clean = _clean(
        "evidence", evidence_ref, required=action is ReviewAction.APPROVE, limit=MAX_EVIDENCE_LENGTH
    )
    return reviewer_clean or "", reason_clean or "", evidence_clean
