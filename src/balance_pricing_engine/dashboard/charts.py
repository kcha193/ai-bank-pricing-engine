"""Altair chart specs. Inputs are view-model data only; no market maths here."""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

import altair as alt
import pandas as pd

from balance_pricing_engine.dashboard.models import (
    BankPosition,
    CurvePoint,
    HistoryPoint,
    MarketPoint,
    TimelineEvent,
)

ROLE_COLOURS = {"Leader": "#1b6ca8", "Selected": "#c0392b", "Other": "#7f8c8d"}


def position_dot_plot(
    positions: Sequence[BankPosition], median_pct: float, selected_bank: Optional[str] = None
) -> alt.LayerChart:
    """Where each bank sits on one term, with market median and leader/selected emphasised."""
    frame = pd.DataFrame(
        [
            {
                "bank": p.bank,
                "rate": p.rate_pct,
                "role": (
                    "Selected"
                    if p.bank == selected_bank
                    else ("Leader" if p.rank == 1 else "Other")
                ),
            }
            for p in positions
        ]
    )
    base = alt.Chart(frame).encode(
        x=alt.X("rate:Q", title="Advertised rate (%)", scale=alt.Scale(zero=False)),
        y=alt.Y("bank:N", sort="x", title=None),
    )
    dots = base.mark_circle(size=140).encode(
        color=alt.Color(
            "role:N",
            scale=alt.Scale(domain=list(ROLE_COLOURS), range=list(ROLE_COLOURS.values())),
            legend=alt.Legend(title=None),
        ),
        tooltip=["bank", alt.Tooltip("rate:Q", format=".2f"), "role"],
    )
    median = (
        alt.Chart(pd.DataFrame({"rate": [median_pct]}))
        .mark_rule(strokeDash=[4, 4])
        .encode(x="rate:Q")
    )
    return (median + dots).properties(height=40 * max(len(frame), 3))


def history_lines(points: Sequence[HistoryPoint]) -> alt.Chart:
    """Every selected bank on one chart; a step line, since rates hold until they change."""
    frame = pd.DataFrame([{"day": p.day, "bank": p.bank, "rate": p.rate_pct} for p in points])
    return (
        alt.Chart(frame)
        .mark_line(point=True, interpolate="step-after")
        .encode(
            x=alt.X("day:T", title=None),
            y=alt.Y("rate:Q", title="Advertised rate (%)", scale=alt.Scale(zero=False)),
            color=alt.Color("bank:N", legend=alt.Legend(title=None)),
            tooltip=["bank", "day:T", alt.Tooltip("rate:Q", format=".2f")],
        )
    )


_MARKET_SERIES = {
    "median": ("median_pct", "Market median (%)"),
    "leader": ("leader_pct", "Market leader (%)"),
    "spread": ("spread_bps", "Spread (bps)"),
}


def market_trend(market: Sequence[MarketPoint], series: str) -> alt.Chart:
    """Market median, leader rate or spread over time, from backend per-date statistics."""
    field, title = _MARKET_SERIES[series]
    frame = pd.DataFrame([{"day": m.day, "value": getattr(m, field)} for m in market])
    return (
        alt.Chart(frame)
        .mark_line(point=True, interpolate="step-after")
        .encode(
            x=alt.X("day:T", title=None),
            y=alt.Y("value:Q", title=title, scale=alt.Scale(zero=series == "spread")),
        )
    )


def repricing_timeline(events: Sequence[TimelineEvent]) -> alt.Chart:
    """Recorded rate moves on a time axis. Order does not imply causation."""
    frame = pd.DataFrame(
        [{"when": e.detected_at, "bank": e.bank, "bps": e.change_bps} for e in events]
    )
    return (
        alt.Chart(frame)
        .mark_circle(size=120)
        .encode(
            x=alt.X("when:T", title=None),
            y=alt.Y("bank:N", title=None),
            color=alt.Color("bps:Q", legend=alt.Legend(title="bps")),
            tooltip=["bank", "when:T", "bps"],
        )
    )


def objective_curve(
    curve: Sequence[CurvePoint], markers: Mapping[str, float]
) -> alt.LayerChart | alt.FacetChart:
    """Modelled objective across our candidate rates, with reference rates marked.

    ``curve`` rates are fractions (as the optimiser reports) and are plotted in percent;
    ``markers`` are already percent (current rate, market median/leader, model optimum).
    """
    frame = pd.DataFrame([{"rate": p.rate * 100, "objective": p.objective} for p in curve])
    line = (
        alt.Chart(frame)
        .mark_line(point=True)
        .encode(
            x=alt.X("rate:Q", title="Our home-loan rate (%)", scale=alt.Scale(zero=False)),
            y=alt.Y("objective:Q", title="Modelled objective ($)"),
        )
    )
    layers = [line]
    if markers:
        rules = (
            alt.Chart(
                pd.DataFrame([{"marker": name, "rate": rate} for name, rate in markers.items()])
            )
            .mark_rule(strokeDash=[4, 4])
            .encode(
                x="rate:Q",
                color=alt.Color("marker:N", legend=alt.Legend(title=None)),
                tooltip=["marker", alt.Tooltip("rate:Q", format=".2f")],
            )
        )
        layers.append(rules)
    return alt.layer(*layers)
