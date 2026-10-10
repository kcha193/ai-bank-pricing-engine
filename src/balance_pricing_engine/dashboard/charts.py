"""Altair chart specs. Inputs are view-model data only; no market maths here."""

from __future__ import annotations

from typing import Optional, Sequence

import altair as alt
import pandas as pd

from balance_pricing_engine.dashboard.models import BankPosition

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
