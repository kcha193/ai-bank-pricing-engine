"""Market Watch: where does each bank sit competitively?"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from balance_pricing_engine.dashboard.charts import position_dot_plot
from balance_pricing_engine.dashboard.formatting import (
    fmt_bps,
    fmt_freshness,
    fmt_nz_datetime,
    fmt_rate_pct,
    fmt_term,
)
from balance_pricing_engine.dashboard.pages._common import cached, unwrap
from balance_pricing_engine.dashboard.services import DashboardServices


def render(services: DashboardServices) -> None:
    overview = unwrap(cached(services, "overview"))
    if overview is None or not overview.matrix.terms:
        return
    terms = list(overview.matrix.terms)
    labels = [fmt_term(t) for t in terms]
    left, right = st.columns(2)
    chosen = left.selectbox(
        "Term",
        labels,
        index=terms.index(overview.default_term) if overview.default_term in terms else 0,
        key="market_watch_term",
    )
    term = terms[labels.index(chosen)]
    positions = unwrap(cached(services, "bank_positions", term))
    summary = unwrap(cached(services, "term_summary", term))
    if positions is None or summary is None:
        return
    bank = right.selectbox("Highlight bank", [p.bank for p in positions], key="market_watch_bank")
    st.caption("Comparable tier: advertised specials. Stale observations are excluded.")

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Bank": p.bank,
                    "Rank": p.rank,
                    "Rate": fmt_rate_pct(p.rate_pct),
                    "vs leader": fmt_bps(p.vs_leader_bps),
                    "vs median": fmt_bps(p.vs_median_bps),
                    "Last change": (
                        fmt_nz_datetime(p.last_change_at)
                        if p.last_change_at
                        else "No recent change"
                    ),
                    "Freshness": fmt_freshness(p.freshness),
                }
                for p in positions
            ]
        ),
        hide_index=True,
        width="stretch",
    )

    selected = next(p for p in positions if p.bank == bank)
    cols = st.columns(5)
    cols[0].metric("Current", fmt_rate_pct(selected.rate_pct))
    cols[1].metric("Rank", f"#{selected.rank}")
    cols[2].metric("vs leader", fmt_bps(selected.vs_leader_bps))
    cols[3].metric("vs median", fmt_bps(selected.vs_median_bps))
    cols[4].metric("Freshness", fmt_freshness(selected.freshness))

    st.altair_chart(position_dot_plot(positions, summary.median_pct, bank), width="stretch")
    st.caption("Dashed line: market median. Blue: leader. Red: highlighted bank.")
