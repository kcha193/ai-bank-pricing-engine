"""Daily and weekly intelligence briefs.

Pure string builders over data the rest of the package already computes
(``MarketSnapshot``, ``MarketStateAssessment``, ``RateChange``,
``CollectorHealth``, ``RunRecord``) -- nothing here queries the store or the
network directly, so these are unit-testable on plain fixtures and reusable
from the CLI, the scheduled workflow, and (via ``alerts.plain_alert``) a
Telegram/webhook delivery.

Deliberately conservative about historical metrics per the project's own
"do not overinterpret sparse history" guidance (see docs/pricing_intelligence.md's
Operations section): a "most active term" is reported from *this week's*
changes plus the *current* snapshot, not a reconstructed before/after median
that would need daily snapshot history this layer does not yet retain.
"first-mover frequency" / "rate leadership duration" (mentioned only as
future extension points) are intentionally not attempted here.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Sequence

from balance_pricing_engine.pricing_intelligence.alerts import term_name
from balance_pricing_engine.pricing_intelligence.changes import RateChange
from balance_pricing_engine.pricing_intelligence.market_state import MarketStateAssessment
from balance_pricing_engine.pricing_intelligence.snapshot import MarketSnapshot
from balance_pricing_engine.pricing_intelligence.store import CollectorHealth, RunRecord


def build_daily_brief(
    *,
    as_of: datetime,
    today_changes: Sequence[RateChange],
    week_assessment: MarketStateAssessment,
    snapshot: MarketSnapshot,
    collector_health: Sequence[CollectorHealth],
    tracked_banks: Sequence[str],
    scenario_triggered_terms: Sequence[int] = (),
) -> str:
    """Render the daily brief for the run just completed.

    ``today_changes`` should be exactly the changes the triggering run
    detected (``OperationalRunResult.outcome.changes``), not a re-query --
    "today" means this run, not a rolling 24h window that could straddle two
    scheduled runs.
    """
    rate_moves = [change for change in today_changes if change.is_rate_move]
    if not rate_moves:
        return (
            "NZ PRICING INTELLIGENCE\n"
            f"{as_of:%d %b %Y}\n\n"
            "No material mortgage pricing changes detected today."
        )

    lines = ["NZ PRICING INTELLIGENCE", f"{as_of:%d %b %Y}", ""]
    lines.append(f"Market state: {week_assessment.state.value}")
    lines.append("")
    lines.append("TODAY")
    for change in sorted(rate_moves, key=lambda c: (-abs(c.change_bps or 0), c.bank)):
        label = f"{change.bank} {term_name(change.term_months)}"
        lines.append(f"{label:<14}{change.change_bps:+.0f} bps")

    lines.append("")
    lines.append(f"{week_assessment.activity.lookback_days}-DAY MARKET")
    lines.append(f"Changes: {week_assessment.changes_7d}")
    lines.append(f"Banks repricing: {week_assessment.banks_changed_7d}")
    lines.append(f"Dominant direction: {week_assessment.dominant_direction}")
    lines.append(f"Mean movement: {week_assessment.mean_change_bps:+.0f} bps")

    leaders = [
        (term, market.market_leader)
        for term, market in sorted(snapshot.terms.items())
        if market.market_leader is not None
    ]
    if leaders:
        lines.append("")
        lines.append("MOST COMPETITIVE")
        for term, leader in leaders:
            lines.append(f"{term_name(term)}: {leader}")

    if snapshot.terms:
        widest_term, widest_market = max(
            snapshot.terms.items(), key=lambda item: item[1].spread_bps
        )
        lines.append("")
        lines.append("WIDEST SPREAD")
        lines.append(f"{term_name(widest_term)}: {widest_market.spread_bps:.0f} bps")

    if tracked_banks:
        fresh = sum(1 for health in collector_health if health.consecutive_failures == 0)
        lines.append("")
        lines.append("COLLECTOR HEALTH")
        lines.append(f"{fresh}/{len(tracked_banks)} banks fresh")

    if scenario_triggered_terms:
        lines.append("")
        lines.append("Pricing scenario analysis triggered for:")
        lines.append(", ".join(term_name(term) for term in scenario_triggered_terms))

    return "\n".join(lines)


def build_weekly_brief(
    *,
    as_of: datetime,
    week_changes: Sequence[RateChange],
    week_assessment: MarketStateAssessment,
    snapshot: MarketSnapshot,
    run_records: Sequence[RunRecord],
) -> str:
    """Render the weekly summary.

    ``run_records`` should already be scoped to the lookback window (e.g.
    ``store.get_recent_run_records(product_type, limit=N)`` filtered by the
    caller to ``started_at >= as_of - 7 days``) -- collector uptime is
    computed only over those.
    """
    rate_moves = [change for change in week_changes if change.is_rate_move]
    lines = ["NZ MORTGAGE PRICING -- WEEKLY", "", f"Market activity: {week_assessment.state.value}"]

    if not rate_moves:
        lines.append("")
        lines.append("No repricing events this week.")
    else:
        banks_changing = {change.bank for change in rate_moves}
        lines.append("")
        lines.append(f"Repricing events: {len(rate_moves)}")
        lines.append(f"Banks changing: {len(banks_changing)}")
        lines.append(f"Dominant direction: {week_assessment.dominant_direction}")

        largest = max(rate_moves, key=lambda c: abs(c.change_bps or 0))
        lines.append("")
        lines.append("Largest move:")
        lines.append(
            f"{largest.bank} {term_name(largest.term_months)} {largest.change_bps:+.0f} bps"
        )

        term_counts = Counter(change.term_months for change in rate_moves)
        most_active_term, _ = term_counts.most_common(1)[0]
        lines.append("")
        lines.append(f"Most active term: {term_name(most_active_term)}")
        market = snapshot.term(most_active_term) if most_active_term is not None else None
        if market is not None:
            lines.append(f"{term_name(most_active_term)} median now: {market.median_rate:.2f}%")

    if run_records:
        lines.append("")
        lines.append("Collector uptime:")
        attempts: Counter = Counter()
        successes: Counter = Counter()
        for record in run_records:
            for bank in record.banks_attempted:
                attempts[bank] += 1
            for bank in record.banks_successful:
                successes[bank] += 1
        for bank in sorted(attempts):
            uptime = 100.0 * successes[bank] / attempts[bank] if attempts[bank] else 0.0
            lines.append(f"{bank:<12}{uptime:>5.0f}%")

    return "\n".join(lines)
