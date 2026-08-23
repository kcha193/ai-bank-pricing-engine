"""Alert formatting for detected market changes.

Formatting is separated from delivery on purpose. This module turns a
:class:`RateChange` into a rendered alert; where that alert goes -- console,
Telegram, email, or the existing in-process event publisher -- is a sink's
problem. No delivery integration exists in this repository yet, so the console
sink is the only concrete one and the protocol is the extension point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, runtime_checkable

from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.changes import ChangeType, RateChange
from balance_pricing_engine.pricing_intelligence.snapshot import MarketSnapshot

_SEVERITY_MARKERS = {
    ChangeType.RATE_DECREASE: "🔴",
    ChangeType.RATE_INCREASE: "🔵",
    ChangeType.NEW_PRODUCT: "🟢",
    ChangeType.REMOVED_PRODUCT: "⚫",
    ChangeType.ELIGIBILITY_CHANGE: "🟡",
    ChangeType.PROMOTION_CHANGE: "🟡",
}

_TERM_NAMES = {
    6: "6 Month",
    12: "1 Year",
    18: "18 Month",
    24: "2 Year",
    36: "3 Year",
    48: "4 Year",
    60: "5 Year",
}


@dataclass(frozen=True)
class Alert:
    """A rendered, delivery-agnostic alert."""

    title: str
    body: str
    change: RateChange

    def render(self) -> str:
        return f"{self.title}\n\n{self.body}"


@runtime_checkable
class AlertSink(Protocol):
    """Where rendered alerts are delivered."""

    def send(self, alert: Alert) -> None: ...


class ConsoleAlertSink:
    """Prints alerts to stdout. The default sink for CLI use."""

    def send(self, alert: Alert) -> None:
        print(alert.render())
        print()


class CollectingAlertSink:
    """Collects alerts in memory. Used by tests and by the dashboard."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


def term_name(term_months: Optional[int]) -> str:
    if term_months is None:
        return "Floating"
    return _TERM_NAMES.get(term_months, f"{term_months} Month")


def format_alert(
    change: RateChange,
    snapshot: Optional[MarketSnapshot] = None,
    previous_snapshot: Optional[MarketSnapshot] = None,
) -> Alert:
    """Render one change as an alert, enriched with market context when available.

    Rank movement is only shown when both snapshots are supplied, because a rank
    change cannot be inferred from a rate change alone -- the rest of the market
    may have moved too.
    """
    marker = _SEVERITY_MARKERS.get(change.change_type, "⚪")
    label = term_name(change.term_months)
    category = change.rate_category.replace("_", " ").title()

    title = f"{marker} NZ MORTGAGE RATE CHANGE"
    lines = [f"{change.bank} — {label} Fixed {category}", ""]

    if change.is_rate_move:
        lines.append(f"{change.previous_rate_pct:.2f}% → {change.current_rate_pct:.2f}%")
        lines.append(f"Movement: {change.change_bps:+.0f} bps")
    elif change.change_type is ChangeType.NEW_PRODUCT:
        lines.append(f"New product advertised at {change.current_rate_pct:.2f}%")
    elif change.change_type is ChangeType.REMOVED_PRODUCT:
        lines.append(f"Withdrawn (last advertised {change.previous_rate_pct:.2f}%)")
    else:
        lines.append(change.change_type.value.replace("_", " ").title())
        if change.detail:
            lines.append(change.detail)

    if change.suspicious:
        lines.extend(["", "⚠️  FLAGGED SUSPICIOUS — verify against the source page", ""])

    rank_line = _rank_movement(change, snapshot, previous_snapshot)
    if rank_line:
        lines.extend(["", "Market rank:", rank_line])

    market_lines = _market_context(change, snapshot)
    if market_lines:
        lines.append("")
        lines.extend(market_lines)

    lines.append("")
    lines.append(f"Detected: {change.detected_at.strftime('%d %b %Y')}")
    if change.source_url:
        lines.append(f"Source: {change.source_url}")

    return Alert(title=title, body="\n".join(lines), change=change)


def _rank_movement(
    change: RateChange,
    snapshot: Optional[MarketSnapshot],
    previous_snapshot: Optional[MarketSnapshot],
) -> Optional[str]:
    if snapshot is None or previous_snapshot is None or change.term_months is None:
        return None
    # A snapshot built for a different rate_category (e.g. the CLI's default
    # "special" snapshot passed alongside a "standard" change) ranks a
    # different market entirely; the rank shown must not be borrowed from it.
    if snapshot.rate_category != change.rate_category:
        return None
    if previous_snapshot.rate_category != change.rate_category:
        return None
    current_market = snapshot.term(change.term_months)
    previous_market = previous_snapshot.term(change.term_months)
    if current_market is None or previous_market is None:
        return None
    current_rank = current_market.rank_for(change.bank)
    previous_rank = previous_market.rank_for(change.bank)
    if current_rank is None or previous_rank is None:
        return None
    if current_rank == previous_rank:
        return f"#{current_rank} (unchanged)"
    return f"#{previous_rank} → #{current_rank}"


def _market_context(
    change: RateChange, snapshot: Optional[MarketSnapshot]
) -> list[str]:
    if snapshot is None or change.term_months is None or change.current_rate_pct is None:
        return []
    # Same guard as _rank_movement(): a snapshot built for a different
    # rate_category compares this change against the wrong market's median.
    if snapshot.rate_category != change.rate_category:
        return []
    market = snapshot.term(change.term_months)
    if market is None:
        return []
    difference = rate_change_bps(market.median_rate, change.current_rate_pct)
    return [
        f"Market median: {market.median_rate:.2f}%",
        f"Difference vs median: {difference:+.0f} bps",
    ]


def format_alerts(
    changes: Sequence[RateChange],
    snapshot: Optional[MarketSnapshot] = None,
    previous_snapshot: Optional[MarketSnapshot] = None,
) -> list[Alert]:
    """Render every change as an alert, most material movement first."""
    ordered = sorted(
        changes,
        key=lambda change: (-abs(change.change_bps or 0), change.bank),
    )
    return [format_alert(change, snapshot, previous_snapshot) for change in ordered]


def dispatch_alerts(alerts: Sequence[Alert], sink: AlertSink) -> int:
    """Deliver alerts through a sink, returning how many were sent."""
    for alert in alerts:
        sink.send(alert)
    return len(alerts)
