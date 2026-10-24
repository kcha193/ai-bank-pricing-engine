"""Command Centre key insights: deterministic sentences templated from backend view models.

No LLM, no narrative and no causes or recommendations: each sentence restates a number the
services already computed (a ranking, a bps gap, an event count, an exclusion). If a fact is not
available, the sentence is simply omitted.
"""

from __future__ import annotations

from typing import Optional, Sequence

from balance_pricing_engine.dashboard.formatting import fmt_rate_pct, fmt_term
from balance_pricing_engine.dashboard.models import BankPosition, Overview, TermSummary

MAX_INSIGHTS = 4


def _join(names: Sequence[str]) -> str:
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _gap_sentence(position: BankPosition, term: str, bank_count: int) -> str:
    rank = f"rank #{position.rank} of {bank_count}"
    if position.rank == 1:
        return f"{position.bank} leads the {term} market at {fmt_rate_pct(position.rate_pct)} ({rank})."
    gap = position.vs_leader_bps
    side = "above" if gap > 0 else "below"
    return (
        f"{position.bank} is {abs(gap):.0f} bps {side} the {term} market leader "
        f"at {fmt_rate_pct(position.rate_pct)} ({rank})."
    )


def build_insights(
    overview: Overview,
    summary: TermSummary,
    positions: Sequence[BankPosition],
    selected_bank: Optional[str],
) -> list[str]:
    """Two to four factual observations for the selected term, most decision-relevant first."""
    term = fmt_term(summary.term_months)
    insights: list[str] = []

    selected = next((p for p in positions if p.bank == selected_bank), None)
    if selected is not None:
        insights.append(_gap_sentence(selected, term, summary.banks_observed))

    leaders = _join(summary.leaders)
    verb = "lead" if len(summary.leaders) > 1 else "leads"
    insights.append(
        f"{leaders} {verb} the {term} market at {fmt_rate_pct(summary.lowest_pct)}; the median is "
        f"{fmt_rate_pct(summary.median_pct)} and the spread across "
        f"{summary.banks_observed} banks is {summary.spread_bps:.0f} bps."
    )

    if overview.changes_7d == 0:
        insights.append("No confirmed repricing events were recorded in the last 7 days.")
    else:
        banks = "bank" if overview.banks_repricing_7d == 1 else "banks"
        moves = "rate move" if overview.changes_7d == 1 else "rate moves"
        direction = overview.dominant_direction.lower()
        tail = f"; the dominant direction is {direction}" if direction not in ("", "flat") else ""
        insights.append(
            f"{overview.changes_7d} confirmed {moves} by {overview.banks_repricing_7d} {banks} "
            f"in the last 7 days{tail}."
        )

    stale = sorted({e.bank for e in overview.exclusions if e.term_months == summary.term_months})
    if stale:
        noun = "bank is" if len(stale) == 1 else "banks are"
        insights.append(
            f"{len(stale)} {noun} excluded from the {term} comparison because the latest data is "
            f"stale: {_join(stale)}."
        )

    quarantined = sorted(p.bank for p in positions if p.quarantined)
    if quarantined:
        insights.append(
            f"The latest {term} reading for {_join(quarantined)} was quarantined; the last "
            "confirmed rate is shown instead."
        )
    return insights[:MAX_INSIGHTS]
