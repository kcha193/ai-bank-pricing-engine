"""Altair chart specs. Inputs are view-model data only; no market maths here.

Every chart follows the same conventions (see ``theme``): rates in percent on a non-zero axis,
basis points on a zero-based axis, Pacific/Auckland-agnostic dates (the services already bucket
them), consistent bank colours, readable 12 px axis labels, and a tooltip on every mark.
Percentages and basis points are never put on the same axis.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

import altair as alt
import pandas as pd

from balance_pricing_engine.dashboard import theme
from balance_pricing_engine.dashboard.formatting import fmt_term
from balance_pricing_engine.dashboard.models import (
    BankPosition,
    ChangeRow,
    CurvePoint,
    HistoryPoint,
    MarketMatrix,
    MarketPoint,
    ScenarioRowView,
    TimelineEvent,
)
from balance_pricing_engine.pricing_intelligence.health import FreshnessState, SystemHealth

ROLE_COLOURS = theme.ROLE_COLOURS
RATE_AXIS = "Advertised rate (%)"


def _finish(chart, *, height: Optional[int] = None):
    """Shared axis/legend/view styling. Applied once, to the outermost chart."""
    if height is not None:
        chart = chart.properties(height=height)
    return (
        chart.configure_axis(
            labelFontSize=theme.AXIS_LABEL_SIZE,
            titleFontSize=theme.AXIS_TITLE_SIZE,
            titleFontWeight=600,
            gridOpacity=0.35,
            domain=False,
            tickColor=theme.MUTED,
        )
        .configure_legend(
            orient="top",
            labelFontSize=12,
            titleFontSize=12,
            symbolSize=110,
            padding=4,
            labelLimit=180,
            columns=2,
        )
        .configure_view(strokeWidth=0)
    )


def _time_x(field: str, *, padded: bool = False) -> alt.X:
    """A date axis with at most one tick per day, thinned when the range is long."""
    return alt.X(
        field,
        title=None,
        scale=alt.Scale(padding=28) if padded else alt.Undefined,
        axis=alt.Axis(
            format="%d %b",
            labelAngle=0,
            tickCount={"interval": "day", "step": 1},
            labelOverlap="greedy",
            labelSeparation=12,
        ),
    )


def _role(position: BankPosition, selected_bank: Optional[str]) -> str:
    if position.bank == selected_bank:
        return "Selected"
    return "Leader" if position.rank == 1 else "Other"


def position_dot_plot(
    positions: Sequence[BankPosition], median_pct: float, selected_bank: Optional[str] = None
):
    """Lollipop ranking for one term: each bank's stem runs from the market median to its rate.

    Banks are ordered by the backend rank (leader on top). The leader is teal, the highlighted
    bank blue; a dashed rule marks the median. Each dot carries its rate as a text label, so
    the chart reads without hovering.
    """
    frame = pd.DataFrame(
        [
            {
                "bank": p.bank,
                "rate": p.rate_pct,
                "rank": p.rank,
                "median": median_pct,
                "role": _role(p, selected_bank),
                "leader": p.rank == 1,
                "label": (
                    f"{'◆ ' if p.rank == 1 else ''}{p.rate_pct:.2f}%"
                    + (" ⚠" if p.quarantined else "")
                ),
                "vs_leader": p.vs_leader_bps,
                "vs_median": p.vs_median_bps,
                "quarantined": "Yes — last confirmed rate" if p.quarantined else "No",
            }
            for p in positions
        ]
    )
    rates = list(frame["rate"]) + [median_pct]
    pad = max((max(rates) - min(rates)) * 0.25, 0.05)
    # Leave enough space for direct labels, including a quarantined leader, at narrow widths.
    x_scale = alt.Scale(domain=[min(rates) - pad, max(rates) + pad * 2], zero=False, nice=False)
    y = alt.Y(
        "bank:N",
        sort=alt.EncodingSortField("rank", order="ascending"),
        title=None,
        axis=alt.Axis(labelFontWeight=600, labelPadding=8),
    )
    tooltip = [
        alt.Tooltip("bank:N", title="Bank"),
        alt.Tooltip("rank:Q", title="Rank"),
        alt.Tooltip("rate:Q", title="Rate (%)", format=".2f"),
        alt.Tooltip("vs_leader:Q", title="vs leader (bps)", format="+.0f"),
        alt.Tooltip("vs_median:Q", title="vs median (bps)", format="+.0f"),
        alt.Tooltip("quarantined:N", title="Quarantined"),
    ]
    colour = alt.Color(
        "role:N",
        scale=alt.Scale(domain=list(ROLE_COLOURS), range=list(ROLE_COLOURS.values())),
        legend=alt.Legend(title=None),
    )
    base = alt.Chart(frame).encode(y=y)
    stems = base.mark_rule(strokeWidth=3, opacity=0.55).encode(
        x=alt.X(
            "median:Q",
            scale=x_scale,
            title=RATE_AXIS,
            axis=alt.Axis(format=".2f", tickCount=4, labelFlush=False),
        ),
        x2="rate:Q",
        color=colour,
    )
    dots = base.mark_circle(size=230, opacity=1, stroke="white", strokeWidth=1.5).encode(
        x=alt.X("rate:Q", scale=x_scale), color=colour, tooltip=tooltip
    )
    labels = base.mark_text(align="left", dx=16, fontSize=12, fontWeight=600).encode(
        x="rate:Q", text="label:N", tooltip=tooltip
    )
    median = (
        alt.Chart(pd.DataFrame({"rate": [median_pct], "text": [f"Median {median_pct:.2f}%"]}))
        .mark_rule(strokeDash=[5, 4], color=theme.SLATE, strokeWidth=1.5)
        .encode(x="rate:Q", tooltip=[alt.Tooltip("rate:Q", title="Market median (%)")])
    )
    median_label = (
        alt.Chart(pd.DataFrame({"rate": [median_pct], "text": [f"Median {median_pct:.2f}%"]}))
        .mark_text(align="left", dx=4, dy=-6, fontSize=11, baseline="bottom")
        .encode(x="rate:Q", y=alt.value(0), text="text:N")
    )
    height = theme.CHART_ROW_HEIGHT * max(len(frame), 3) + theme.CHART_CHROME
    return _finish(alt.layer(median, stems, dots, labels, median_label), height=height)


def gap_bars(positions: Sequence[BankPosition], selected_bank: Optional[str] = None):
    """Each bank's gap to the leader and to the median, in bps (backend-computed values)."""
    rows = []
    for p in positions:
        for measure, value in (("vs leader", p.vs_leader_bps), ("vs median", p.vs_median_bps)):
            rows.append(
                {
                    "bank": p.bank,
                    "rank": p.rank,
                    "measure": measure,
                    "bps": value,
                    "label": f"{value:+.0f}" if value else "0",
                    "selected": p.bank == selected_bank,
                }
            )
    frame = pd.DataFrame(rows)
    y = alt.Y(
        "bank:N",
        sort=alt.EncodingSortField("rank", order="ascending"),
        title=None,
        axis=alt.Axis(labelFontWeight=600, labelPadding=8),
    )
    colour = alt.Color(
        "measure:N",
        scale=alt.Scale(domain=["vs leader", "vs median"], range=[theme.TEAL, theme.SLATE]),
        legend=alt.Legend(title=None),
    )
    tooltip = [
        alt.Tooltip("bank:N", title="Bank"),
        alt.Tooltip("measure:N", title="Gap"),
        alt.Tooltip("bps:Q", title="bps", format="+.0f"),
    ]
    base = alt.Chart(frame).encode(y=y, yOffset=alt.YOffset("measure:N"))
    bars = base.mark_bar(cornerRadiusEnd=3, height=11).encode(
        x=alt.X("bps:Q", title="Gap (bps) — positive means a higher rate"),
        color=colour,
        opacity=alt.condition(alt.datum.selected, alt.value(1.0), alt.value(0.75)),
        tooltip=tooltip,
    )
    text = base.mark_text(align="left", dx=4, fontSize=11).encode(
        x="bps:Q", text="label:N", tooltip=tooltip
    )
    zero = alt.Chart(pd.DataFrame({"bps": [0]})).mark_rule(color=theme.SLATE).encode(x="bps:Q")
    height = (theme.CHART_ROW_HEIGHT + 8) * max(len(positions), 3) + theme.CHART_CHROME
    return _finish(alt.layer(zero, bars, text), height=height)


def term_structure(
    matrix: MarketMatrix,
    banks: Optional[Sequence[str]] = None,
    selected_bank: Optional[str] = None,
):
    """Each bank's advertised rate across terms, from the market matrix.

    Stale cells are left out (they are excluded from every statistic); quarantined fallback
    rates are drawn hollow and say so in the tooltip. A missing term breaks the line instead of
    being bridged.
    """
    colours = theme.bank_colours(matrix.banks)
    chosen = list(banks) if banks is not None else list(matrix.banks)
    rows = []
    for bank in chosen:
        for term in matrix.terms:
            cell = matrix.cells.get((bank, term))
            usable = cell is not None and cell.rate_pct is not None and not cell.stale
            rows.append(
                {
                    "bank": bank,
                    "term_months": term,
                    "term": fmt_term(term),
                    "rate": cell.rate_pct if usable else None,
                    "status": (
                        "Quarantined — last confirmed rate"
                        if usable and cell.quarantined
                        else ("Term leader" if usable and cell.is_leader else "Current")
                    ),
                    "selected": bank == selected_bank,
                }
            )
    frame = pd.DataFrame(rows)
    order = [fmt_term(t) for t in matrix.terms]
    x = alt.X("term:O", sort=order, title="Term", axis=alt.Axis(labelAngle=0))
    colour = alt.Color(
        "bank:N",
        scale=alt.Scale(domain=chosen, range=[colours[b] for b in chosen]),
        legend=alt.Legend(title=None),
    )
    tooltip = [
        alt.Tooltip("bank:N", title="Bank"),
        alt.Tooltip("term:N", title="Term"),
        alt.Tooltip("rate:Q", title="Rate (%)", format=".2f"),
        alt.Tooltip("status:N", title="Status"),
    ]
    base = alt.Chart(frame).encode(
        x=x, y=alt.Y("rate:Q", title=RATE_AXIS, scale=alt.Scale(zero=False)), color=colour
    )
    lines = base.mark_line(invalid="break-paths-show-domains").encode(
        strokeWidth=alt.condition(alt.datum.selected, alt.value(4), alt.value(2)),
        opacity=alt.condition(
            alt.datum.selected if selected_bank else alt.datum.rate != None,  # noqa: E711
            alt.value(1.0),
            alt.value(0.55),
        ),
    )
    points = base.mark_point(size=70, filled=True).encode(
        tooltip=tooltip,
        fill=alt.condition(
            alt.datum.status == "Quarantined — last confirmed rate", alt.value("white"), colour
        ),
    )
    return _finish(alt.layer(lines, points), height=theme.CHART_HEIGHT + 20)


def history_lines(
    points: Sequence[HistoryPoint],
    *,
    days: Sequence = (),
    selected_bank: Optional[str] = None,
    banks: Sequence[str] = (),
):
    """Every selected bank on one step chart, since rates hold until they change.

    ``days`` are all dates the backend series covers; a bank with no rate on one of them gets
    a gap instead of a line bridged across the missing period. The highlighted bank is drawn
    heavier; the others stay readable but recede.
    """
    frame = pd.DataFrame([{"day": p.day, "bank": p.bank, "rate": p.rate_pct} for p in points])
    if days and not frame.empty:
        present = set(zip(frame["day"], frame["bank"]))
        plotted = list(dict.fromkeys(frame["bank"]))
        first_seen = frame.groupby("bank")["day"].min().to_dict()
        gaps = [
            {"day": day, "bank": bank, "rate": None}
            for bank in plotted
            for day in days
            if (day, bank) not in present and day > first_seen[bank]
        ]
        if gaps:
            frame = pd.concat([frame, pd.DataFrame(gaps)], ignore_index=True)
        frame = frame.sort_values(["bank", "day"])
    domain = list(banks) or list(dict.fromkeys(frame["bank"]))
    colours = theme.bank_colours(domain)
    frame["selected"] = frame["bank"] == selected_bank
    colour = alt.Color(
        "bank:N",
        scale=alt.Scale(domain=domain, range=[colours[b] for b in domain]),
        legend=alt.Legend(title=None),
    )
    emphasis = alt.datum.selected if selected_bank else alt.datum.bank != None  # noqa: E711
    base = alt.Chart(frame).encode(
        x=_time_x("day:T"),
        y=alt.Y("rate:Q", title=RATE_AXIS, scale=alt.Scale(zero=False)),
        color=colour,
    )
    line = base.mark_line(interpolate="step-after", invalid="break-paths-show-domains").encode(
        strokeDash=alt.StrokeDash(
            "bank:N",
            scale=alt.Scale(domain=domain, range=list(theme.BANK_DASHES)[: len(domain)]),
            legend=None,
        ),
        strokeWidth=alt.condition(emphasis, alt.value(3.2), alt.value(1.6)),
        opacity=alt.condition(emphasis, alt.value(1.0), alt.value(0.5)),
    )
    dots = base.mark_point(size=48, filled=True).encode(
        shape=alt.Shape("bank:N", legend=None),
        opacity=alt.condition(emphasis, alt.value(1.0), alt.value(0.45)),
        tooltip=[
            alt.Tooltip("bank:N", title="Bank"),
            alt.Tooltip("day:T", title="Date", format="%d %b %Y"),
            alt.Tooltip("rate:Q", title="Rate (%)", format=".2f"),
        ],
    )
    return _finish(alt.layer(line, dots).interactive(bind_y=False), height=theme.CHART_HEIGHT + 40)


def market_levels(market: Sequence[MarketPoint], leader_series: str = "Market leader"):
    """Market leader and median rates over time (percent), from backend per-date statistics.

    ``leader_series`` names the leader line for the product ("Leader (highest rate)" for
    deposits), so the legend never implies one ranking direction for both markets.
    """
    rows = []
    for m in market:
        rows.append({"day": m.day, "series": "Market median", "value": m.median_pct})
        rows.append(
            {
                "day": m.day,
                "series": leader_series,
                "value": m.leader_pct,
                "leaders": ", ".join(m.leaders),
            }
        )
    frame = pd.DataFrame(rows)
    if "leaders" not in frame:
        frame["leaders"] = ""
    frame["leaders"] = frame["leaders"].fillna("")
    chart = (
        alt.Chart(frame)
        .mark_line(interpolate="step-after", strokeWidth=2.5, point=alt.OverlayMarkDef(size=30))
        .encode(
            x=_time_x("day:T"),
            y=alt.Y("value:Q", title=RATE_AXIS, scale=alt.Scale(zero=False)),
            color=alt.Color(
                "series:N",
                scale=alt.Scale(
                    domain=[leader_series, "Market median"], range=[theme.TEAL, theme.CHART_INK]
                ),
                legend=alt.Legend(title=None),
            ),
            strokeDash=alt.StrokeDash(
                "series:N",
                scale=alt.Scale(domain=[leader_series, "Market median"], range=[[1, 0], [6, 3]]),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("series:N", title="Series"),
                alt.Tooltip("day:T", title="Date", format="%d %b %Y"),
                alt.Tooltip("value:Q", title="Rate (%)", format=".2f"),
                alt.Tooltip("leaders:N", title="Leader(s)"),
            ],
        )
    )
    return _finish(chart, height=240)


def market_dispersion(market: Sequence[MarketPoint]):
    """Spread between the highest and lowest advertised rate (bps), on its own zero-based axis."""
    frame = pd.DataFrame([{"day": m.day, "spread": m.spread_bps} for m in market])
    chart = (
        alt.Chart(frame)
        .mark_area(interpolate="step-after", opacity=0.25, color=theme.CHART_BLUE, line=True)
        .encode(
            x=_time_x("day:T"),
            y=alt.Y("spread:Q", title="Dispersion (bps)", scale=alt.Scale(zero=True)),
            tooltip=[
                alt.Tooltip("day:T", title="Date", format="%d %b %Y"),
                alt.Tooltip("spread:Q", title="Spread (bps)", format=".0f"),
            ],
        )
    )
    return _finish(chart, height=200)


def _direction(bps: Optional[float]) -> str:
    if bps is None or bps == 0:
        return "Other change"
    return "Increase" if bps > 0 else "Decrease"


def repricing_timeline(events: Sequence[TimelineEvent], banks: Sequence[str] = ()):
    """Recorded rate moves on a time axis: direction by colour and shape, size by magnitude.

    Order does not imply causation; the page says so next to the chart.
    """
    frame = pd.DataFrame(
        [
            {
                "when": e.detected_at,
                "bank": e.bank,
                "bps": e.change_bps,
                "size": abs(e.change_bps),
                "direction": _direction(e.change_bps),
                "label": f"{e.change_bps:+.0f}",
            }
            for e in events
        ]
    )
    return _finish(_event_layers(frame, banks), height=_lanes_height(frame))


def movement_timeline(rows: Sequence[ChangeRow], banks: Sequence[str] = ()):
    """Every event in the Rate Movements window, one lane per bank."""
    frame = pd.DataFrame(
        [
            {
                "when": r.detected_at,
                "bank": r.bank,
                "bps": r.change_bps,
                "size": abs(r.change_bps) if r.change_bps is not None else 4.0,
                "direction": theme.change_group(r.change_type),
                "term": fmt_term(r.term_months),
                "label": f"{r.change_bps:+.0f}" if r.change_bps else "",
            }
            for r in rows
        ]
    )
    return _finish(_event_layers(frame, banks, with_term=True), height=_lanes_height(frame))


def _lanes_height(frame: pd.DataFrame) -> int:
    lanes = frame["bank"].nunique() if not frame.empty else 1
    return max(180, theme.CHART_ROW_HEIGHT * lanes + theme.CHART_CHROME)


def _event_layers(frame: pd.DataFrame, banks: Sequence[str], *, with_term: bool = False):
    groups = list(theme.DIRECTION_COLOURS)
    shapes = ["triangle-up", "triangle-down", "cross", "square", "circle"]
    lanes = [b for b in banks if b in set(frame["bank"])] or sorted(set(frame["bank"]))
    tooltip = [
        alt.Tooltip("bank:N", title="Bank"),
        alt.Tooltip("when:T", title="Detected", format="%d %b %Y %H:%M"),
        alt.Tooltip("direction:N", title="Event"),
        alt.Tooltip("bps:Q", title="Movement (bps)", format="+.0f"),
    ]
    if with_term:
        tooltip.insert(1, alt.Tooltip("term:N", title="Term"))
    base = alt.Chart(frame).encode(
        x=_time_x("when:T", padded=True),
        y=alt.Y("bank:N", sort=lanes, title=None, axis=alt.Axis(labelFontWeight=600)),
    )
    marks = base.mark_point(filled=True, opacity=0.9).encode(
        color=alt.Color(
            "direction:N",
            scale=alt.Scale(domain=groups, range=[theme.DIRECTION_COLOURS[g] for g in groups]),
            legend=alt.Legend(title=None),
        ),
        shape=alt.Shape("direction:N", scale=alt.Scale(domain=groups, range=shapes), legend=None),
        size=alt.Size(
            "size:Q", scale=alt.Scale(range=[90, 600], zero=True), title="Size: |bps|", legend=None
        ),
        tooltip=tooltip,
    )
    text = base.mark_text(dy=-16, fontSize=11, fontWeight=600).encode(text="label:N")
    return alt.layer(marks, text)


def scenario_rates(rows: Sequence[ScenarioRowView], labels: Mapping[str, str]):
    """Dumbbell: the competitor assumption and the modelled rate for each scenario (percent)."""
    frame = pd.DataFrame(
        [
            {
                "scenario": labels.get(r.label, r.label),
                "order": i,
                "competitor": r.competitor_rate * 100,
                "ours": r.our_rate * 100,
            }
            for i, r in enumerate(rows)
        ]
    )
    long = frame.melt(
        id_vars=["scenario", "order"],
        value_vars=["competitor", "ours"],
        var_name="kind",
        value_name="rate",
    )
    long["kind"] = long["kind"].map(
        {"competitor": "Competitor assumption", "ours": "Modelled optimal rate"}
    )
    order = list(frame["scenario"])
    y = alt.Y(
        "scenario:N", sort=order, title=None, axis=alt.Axis(labelFontWeight=600, labelLimit=220)
    )
    link = (
        alt.Chart(frame)
        .mark_rule(strokeWidth=3, color=theme.MUTED)
        .encode(
            y=y,
            x=alt.X("competitor:Q", title="Rate (%)", scale=alt.Scale(zero=False)),
            x2="ours:Q",
        )
    )
    kinds = ["Competitor assumption", "Modelled optimal rate"]
    dots = (
        alt.Chart(long)
        .mark_circle(size=200, opacity=1)
        .encode(
            y=y,
            x="rate:Q",
            color=alt.Color(
                "kind:N",
                scale=alt.Scale(domain=kinds, range=[theme.SLATE, theme.CHART_BLUE]),
                legend=alt.Legend(title=None),
            ),
            tooltip=[
                alt.Tooltip("scenario:N", title="Scenario"),
                alt.Tooltip("kind:N", title="Rate"),
                alt.Tooltip("rate:Q", title="Rate (%)", format=".2f"),
            ],
        )
    )
    return _finish(
        alt.layer(link, dots),
        height=theme.CHART_ROW_HEIGHT * max(len(frame), 3) + theme.CHART_CHROME,
    )


def objective_curve(
    curve: Sequence[CurvePoint],
    markers: Mapping[str, float],
    rate_title: str = "Our home-loan rate (%)",
):
    """Modelled objective across our candidate rates, with reference rates marked.

    ``curve`` rates are fractions (as the optimiser reports) and are plotted in percent;
    ``markers`` are already percent (current rate, market median/leader, model optimum).
    ``rate_title`` names the side the curve is drawn along (loan or deposit rate).
    """
    frame = pd.DataFrame([{"rate": p.rate * 100, "objective": p.objective} for p in curve])
    line = (
        alt.Chart(frame)
        .mark_line(point=alt.OverlayMarkDef(size=30), strokeWidth=2.5, color=theme.CHART_INK)
        .encode(
            x=alt.X("rate:Q", title=rate_title, scale=alt.Scale(zero=False)),
            y=alt.Y("objective:Q", title="Modelled objective ($)", axis=alt.Axis(format="~s")),
            tooltip=[
                alt.Tooltip("rate:Q", title="Our rate (%)", format=".2f"),
                alt.Tooltip("objective:Q", title="Objective ($)", format=",.0f"),
            ],
        )
    )
    layers = [line]
    if markers:
        names = list(markers)
        palette = [theme.CHART_BLUE, theme.SLATE, theme.TEAL, theme.AMBER, theme.RED]
        rules = (
            alt.Chart(
                pd.DataFrame([{"marker": name, "rate": rate} for name, rate in markers.items()])
            )
            .mark_rule(strokeDash=[5, 4], strokeWidth=2)
            .encode(
                x="rate:Q",
                color=alt.Color(
                    "marker:N",
                    scale=alt.Scale(domain=names, range=palette[: len(names)]),
                    legend=alt.Legend(title=None),
                ),
                tooltip=[
                    alt.Tooltip("marker:N", title="Marker"),
                    alt.Tooltip("rate:Q", title="Rate (%)", format=".2f"),
                ],
            )
        )
        layers.append(rules)
    return _finish(alt.layer(*layers), height=theme.CHART_HEIGHT)


def scenario_objectives(rows: Sequence[ScenarioRowView], labels: Mapping[str, str]):
    """Compare modelled value alongside rate comparisons; do not imply realised profit."""
    frame = pd.DataFrame(
        [
            {
                "scenario": labels.get(r.label, r.label),
                "objective": r.objective,
                "rate": r.our_rate * 100,
            }
            for r in rows
        ]
    )
    chart = (
        alt.Chart(frame)
        .mark_bar(color=theme.CHART_INK, cornerRadiusEnd=3)
        .encode(
            y=alt.Y(
                "scenario:N",
                sort=list(frame["scenario"]),
                title=None,
                axis=alt.Axis(labelLimit=170),
            ),
            x=alt.X(
                "objective:Q",
                title="Modelled risk-adjusted value ($)",
                scale=alt.Scale(zero=True),
                axis=alt.Axis(format="~s", tickCount=4),
            ),
            tooltip=[
                alt.Tooltip("scenario:N", title="Scenario"),
                alt.Tooltip("objective:Q", title="Modelled value ($)", format=",.0f"),
                alt.Tooltip("rate:Q", title="Modelled optimal rate (%)", format=".2f"),
            ],
        )
    )
    return _finish(chart, height=theme.CHART_ROW_HEIGHT * max(len(frame), 3) + theme.CHART_CHROME)


def freshness_bars(health: SystemHealth):
    """Age of each bank's latest validated observation against the freshness limit."""
    states = {
        FreshnessState.FRESH: "Fresh",
        FreshnessState.STALE: "Stale",
        FreshnessState.UNKNOWN: "No data",
    }
    frame = pd.DataFrame(
        [
            {
                "bank": b.bank,
                "age": b.freshness.age_hours if b.freshness.age_hours is not None else 0.0,
                "state": states[b.freshness.state],
                "label": (
                    f"{b.freshness.age_hours:.0f} h"
                    if b.freshness.age_hours is not None
                    else "no observation"
                ),
                "failures": b.consecutive_failures,
            }
            for b in health.banks
        ]
    )
    domain = ["Fresh", "Stale", "No data"]
    y = alt.Y("bank:N", title=None, sort=None, axis=alt.Axis(labelFontWeight=600))
    bars = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=3, height=16)
        .encode(
            y=y,
            x=alt.X(
                "age:Q",
                title="Age of latest validated observation (hours)",
                axis=alt.Axis(tickCount=8),
            ),
            color=alt.Color(
                "state:N",
                scale=alt.Scale(domain=domain, range=[theme.TEAL, theme.AMBER, theme.SLATE]),
                legend=alt.Legend(title=None),
            ),
            tooltip=[
                alt.Tooltip("bank:N", title="Bank"),
                alt.Tooltip("label:N", title="Age"),
                alt.Tooltip("state:N", title="Freshness"),
                alt.Tooltip("failures:Q", title="Consecutive failures"),
            ],
        )
    )
    text = (
        alt.Chart(frame)
        .mark_text(align="left", dx=4, fontSize=11)
        .encode(y=y, x="age:Q", text="label:N")
    )
    limit = health.max_observation_age_hours
    rule = (
        alt.Chart(pd.DataFrame({"age": [limit], "text": [f"Limit {limit:.0f} h"]}))
        .mark_rule(strokeDash=[5, 4], color=theme.RED, strokeWidth=1.5)
        .encode(x="age:Q", tooltip=[alt.Tooltip("text:N", title="Freshness limit")])
    )
    return _finish(
        alt.layer(bars, text, rule),
        height=theme.CHART_ROW_HEIGHT * max(len(frame), 3) + theme.CHART_CHROME,
    )
