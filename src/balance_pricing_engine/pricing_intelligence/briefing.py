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
from datetime import datetime, timedelta
from typing import Optional, Sequence

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.alerts import term_name
from balance_pricing_engine.pricing_intelligence.changes import RateChange
from balance_pricing_engine.pricing_intelligence.market_state import MarketStateAssessment
from balance_pricing_engine.pricing_intelligence.snapshot import MarketSnapshot
from balance_pricing_engine.pricing_intelligence.store import CollectorHealth, RunRecord


def _product_label(product_type: ProductType) -> str:
    """Display label derived from ``snapshot.product_type`` -- both brief
    builders previously hard-coded "MORTGAGE" wording regardless of which
    product the caller actually selected, so a term-deposit brief (--product
    term_deposit) delivered lending-labelled intelligence."""
    return "TERM DEPOSIT" if product_type is ProductType.TERM_DEPOSIT else "MORTGAGE"


def build_daily_brief(
    *,
    as_of: datetime,
    today_changes: Sequence[RateChange],
    week_assessment: MarketStateAssessment,
    snapshot: MarketSnapshot,
    collector_health: Sequence[CollectorHealth],
    tracked_banks: Sequence[str],
    scenario_triggered_terms: Sequence[int] = (),
    max_observation_age_hours: Optional[float] = None,
) -> str:
    """Render the daily brief for the run just completed.

    ``today_changes`` should cover exactly "today" -- the NZ calendar day so
    far (see the CLI's ``_brief``, which queries since NZ-local midnight
    converted to UTC) -- not a rolling 24h UTC window: around the
    Pacific/Auckland DST transition consecutive scheduled runs are only 23
    hours apart, and a delayed or manual run has the same problem any day of
    the year, so a rolling window can re-present yesterday's already-seen
    change under today's TODAY section.

    A suspicious movement is still listed under TODAY -- it is a genuine
    detected event worth an operator's attention -- but flagged, matching
    the "FLAGGED SUSPICIOUS -- verify against the source page" warning a live
    alert already carries for the same change (see ``alerts.format_alert``);
    it is never silently presented as ordinary, trusted repricing.

    ``max_observation_age_hours``, when given, applies the same staleness
    threshold the ``health`` CLI command and dashboard already use: a bank
    that has simply stopped being attempted (scheduling disabled, the
    machine off for a week) never fails again, so ``consecutive_failures``
    alone would count it as fresh indefinitely. Compared against ``as_of``,
    which carries the same instant as the run's UTC "now" just displayed in
    NZ-local time, so the comparison against ``last_success_at`` (UTC) is
    unaffected by the display timezone. Omitted, the count falls back to
    the previous failures-only behaviour.

    A quiet TODAY section never short-circuits the rest of the brief: the
    7-day market context, collector health, and any scenario trigger are
    independent of whether *today* specifically moved, and dropping them on
    a quiet day used to let the commonly-delivered brief say "no material
    changes detected" while masking a tracked bank that is failing or stale,
    or elevated activity from earlier in the week.
    """
    product_label = _product_label(snapshot.product_type)
    rate_moves = [change for change in today_changes if change.is_rate_move]

    lines = [f"NZ {product_label} PRICING INTELLIGENCE", f"{as_of:%d %b %Y}", ""]
    lines.append(f"Market state: {week_assessment.state.value}")
    lines.append("")
    lines.append("TODAY")
    if not rate_moves:
        lines.append(f"No material {product_label.lower()} pricing changes detected today.")
    else:
        for change in sorted(rate_moves, key=lambda c: (-abs(c.change_bps or 0), c.bank)):
            label = f"{change.bank} {term_name(change.term_months)}"
            flag = "  [SUSPICIOUS -- verify source]" if change.suspicious else ""
            lines.append(f"{label:<14}{change.change_bps:+.0f} bps{flag}")

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
        # Filtered to the current tracked-bank set before counting: a bank
        # removed from selected_banks after health history accumulated
        # still has a collector_health row, and counting it in the
        # numerator against a denominator of only currently-tracked banks
        # could otherwise report an impossible "5/4 banks fresh".
        tracked = set(tracked_banks)
        max_age = (
            timedelta(hours=max_observation_age_hours)
            if max_observation_age_hours is not None
            else None
        )

        def _is_fresh(health: CollectorHealth) -> bool:
            if health.consecutive_failures != 0:
                return False
            if max_age is not None and health.last_success_at is not None:
                return as_of - health.last_success_at <= max_age
            return True

        fresh = sum(1 for health in collector_health if health.bank in tracked and _is_fresh(health))
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

    Suspicious movements are excluded from every aggregate stat below
    (repricing-event count, banks changing, largest move, most active term),
    the same as :func:`~.changes.compute_market_activity` already excludes
    them from market-state classification -- a parser fault must not be able
    to fake a repricing round here either. A footnote reports how many were
    excluded so a genuine cluster of suspicious readings is still visible.
    """
    product_label = _product_label(snapshot.product_type)
    all_changes = [change for change in week_changes if change.is_rate_move]
    rate_moves = [change for change in all_changes if not change.suspicious]
    suspicious_count = len(all_changes) - len(rate_moves)
    lines = [
        f"NZ {product_label} PRICING -- WEEKLY",
        "",
        f"Market activity: {week_assessment.state.value}",
    ]

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

    if suspicious_count:
        lines.append("")
        lines.append(
            f"{suspicious_count} suspicious movement(s) excluded above -- verify against "
            "the source page before treating as genuine repricing."
        )

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
