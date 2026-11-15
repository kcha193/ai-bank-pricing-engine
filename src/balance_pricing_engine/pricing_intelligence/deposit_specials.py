"""Comparability policy for term-deposit *special* rates (DATA-001).

The extractor tags a term-deposit row whose text or column heading says "special" with
``rate_category="special"``. The ordinary term-deposit benchmark is built from
``rate_category="term_deposit"`` rows. Before Phase 2b a special therefore vanished from the
benchmark with no trace, and the run still reported success.

A special is a promotional rate. A promotional rate that any depositor who qualifies for the
bank's ordinary rate can also get is a genuine competitor price. A promotion limited to new
money, new customers, larger balances or online applications is not. The page rarely says
which, so this module classifies each special from the evidence the record itself carries
(``raw_text_context``: the row's cells plus its own column heading) and never infers
eligibility the page does not state:

``COMPARABLE``
    The text names public availability (an ``availability_markers`` phrase) and nothing makes
    the special unlike the bank's ordinary rate at that term. It may enter the ordinary
    benchmark, keeping ``rate_category="special"`` so the representative stays explainable.
``NOT_COMPARABLE``
    The text or fields show it is unlike the ordinary rate: a restriction phrase, a PIE or
    foreign-currency marker, a balance tier the ordinary rate does not serve, or a different
    published payment frequency. Recorded and excluded. This is an understood exclusion, so
    it does not degrade the run.
``UNCERTAIN``
    No disqualifying evidence, but no positive evidence of availability either (or a footnote
    marker, or an unpublished payment frequency where the ordinary rate publishes one).
    Recorded and excluded, and the collection is reported as degraded (exit 2) so an operator
    sees it instead of a silent success.

The balance tier is compared by band (``balance_tier_edges``, the same banding as the peer
plausibility check) against the bank's own ordinary rows at the same term; with no ordinary
row at that term the special must sit in the retail band (below the first edge).

Everything here is a pure function of stored record fields, so it works the same on old
records and needs no schema change.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from enum import Enum
import re
from typing import Iterable, Optional, Sequence

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType

#: Category the extractor assigns to a promotional term-deposit row.
SPECIAL_CATEGORY = "special"
#: Category of the ordinary term-deposit benchmark rows.
ORDINARY_CATEGORY = "term_deposit"

_FOOTNOTE_MARKERS = ("*", "†", "‡")
_FOREIGN_CURRENCY_WORDS = ("usd", "aud", "gbp", "eur", "foreign currency")


class SpecialComparability(str, Enum):
    COMPARABLE = "comparable"
    NOT_COMPARABLE = "not_comparable"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class DepositSpecialAssessment:
    """Why one term-deposit special is, is not, or may not be benchmark-comparable."""

    observation_key: str
    bank: str
    term_months: Optional[int]
    rate_pct: float
    status: SpecialComparability
    reasons: tuple[str, ...]
    source_url: str

    def describe(self) -> dict:
        return {
            "bank": self.bank,
            "term_months": self.term_months,
            "rate_pct": self.rate_pct,
            "status": self.status.value,
            "reasons": list(self.reasons),
            "observation_key": self.observation_key,
            "source_url": self.source_url,
        }


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase.lower())}(?!\w)", text) is not None


def _band(amount: Optional[float], edges: Sequence[float]) -> int:
    return bisect_right(list(edges), amount or 0.0)


def _frequency_reasons(
    special: CompetitorRateRecord, ordinary: Sequence[CompetitorRateRecord]
) -> tuple[list[str], list[str]]:
    """``(disqualifying, uncertain)`` reasons from the published payment frequency."""
    published = {r.interest_payment_frequency for r in ordinary}
    if not published or special.interest_payment_frequency in published:
        return [], []
    known = published - {"unknown"}
    if special.interest_payment_frequency != "unknown" and known:
        return ["payment_frequency_differs_from_standard"], []
    return [], ["payment_frequency_unverified"]


def assess_deposit_special(
    special: CompetitorRateRecord,
    ordinary_same_term: Sequence[CompetitorRateRecord],
    *,
    restriction_markers: Sequence[str],
    availability_markers: Sequence[str],
    balance_tier_edges: Sequence[float],
) -> DepositSpecialAssessment:
    """Classify one special against the same bank's ordinary rows at the same term."""
    text = special.raw_text_context.lower()
    disqualifying = [
        f"restriction_marker:{marker.lower()}"
        for marker in restriction_markers
        if _contains(text, marker)
    ]
    if _contains(text, "pie"):
        disqualifying.append("pie_marker")
    if any(_contains(text, word) for word in _FOREIGN_CURRENCY_WORDS):
        disqualifying.append("foreign_currency")
    ordinary_bands = {
        _band(r.min_balance_or_deposit, balance_tier_edges) for r in ordinary_same_term
    } or {0}
    if _band(special.min_balance_or_deposit, balance_tier_edges) not in ordinary_bands:
        disqualifying.append("balance_tier_differs_from_standard")
    frequency_disqualifying, uncertain = _frequency_reasons(special, ordinary_same_term)
    disqualifying.extend(frequency_disqualifying)
    if any(marker in special.raw_text_context for marker in _FOOTNOTE_MARKERS):
        uncertain.append("footnote_marker")
    evidence = [
        f"public_availability_evidence:{marker.lower()}"
        for marker in availability_markers
        if _contains(text, marker)
    ]
    if not evidence:
        uncertain.append("no_public_availability_evidence")

    if disqualifying:
        status, reasons = SpecialComparability.NOT_COMPARABLE, disqualifying + uncertain
    elif uncertain:
        status, reasons = SpecialComparability.UNCERTAIN, uncertain + evidence
    else:
        status, reasons = SpecialComparability.COMPARABLE, evidence
    return DepositSpecialAssessment(
        observation_key=special.observation_key(),
        bank=special.bank,
        term_months=special.term_months_normalised,
        rate_pct=special.advertised_rate_pct,
        status=status,
        reasons=tuple(reasons),
        source_url=special.source_url,
    )


def assess_deposit_specials(
    records: Iterable[CompetitorRateRecord],
    *,
    restriction_markers: Sequence[str],
    availability_markers: Sequence[str],
    balance_tier_edges: Sequence[float],
) -> tuple[DepositSpecialAssessment, ...]:
    """Assess every term-deposit special in ``records``, in a stable order.

    Each special is compared only with ordinary rows from the same input set, so callers pass
    the same records they are about to rank (a run's accepted rows, or a snapshot's trusted
    current state).
    """
    materialised = [r for r in records if ProductType(r.product_type) is ProductType.TERM_DEPOSIT]
    ordinary: dict[tuple[str, Optional[int]], list[CompetitorRateRecord]] = {}
    for record in materialised:
        if record.rate_category == ORDINARY_CATEGORY:
            ordinary.setdefault((record.bank, record.term_months_normalised), []).append(record)
    assessments = [
        assess_deposit_special(
            record,
            ordinary.get((record.bank, record.term_months_normalised), []),
            restriction_markers=restriction_markers,
            availability_markers=availability_markers,
            balance_tier_edges=balance_tier_edges,
        )
        for record in materialised
        if record.rate_category == SPECIAL_CATEGORY
    ]
    return tuple(sorted(assessments, key=lambda a: (a.bank, a.term_months or 0, a.observation_key)))


def comparable_keys(assessments: Iterable[DepositSpecialAssessment]) -> set[str]:
    """Observation keys of the specials admitted to the ordinary term-deposit benchmark."""
    return {a.observation_key for a in assessments if a.status is SpecialComparability.COMPARABLE}
