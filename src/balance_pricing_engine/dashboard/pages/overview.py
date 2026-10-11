"""Overview: what is the market doing right now?"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from balance_pricing_engine.dashboard.formatting import EM_DASH, fmt_rate_pct, fmt_term
from balance_pricing_engine.dashboard.pages._common import (
    cached,
    live_term_selector,
    matrix_frame,
    unwrap,
)
from balance_pricing_engine.dashboard.services import DashboardServices


def render(services: DashboardServices) -> None:
    overview = unwrap(cached(services, "overview"))
    if overview is None:
        return

    cols = st.columns(5)
    cols[0].metric("Changes today", overview.changes_today)
    cols[1].metric("Changes (7 days)", overview.changes_7d)
    cols[2].metric("Banks repricing", overview.banks_repricing_7d)
    cols[3].metric("Dominant direction", overview.dominant_direction.title())
    cols[4].metric(
        "Most active term",
        fmt_term(overview.most_active_term) if overview.most_active_term else EM_DASH,
    )
    st.caption(overview.market_state_rationale)

    st.subheader("Mortgage market (advertised specials)")
    st.dataframe(matrix_frame(overview.matrix), width="stretch")
    st.caption("◆ term leader · ⚠ stale (excluded from rankings and statistics) · — not available")
    for exclusion in overview.exclusions:
        st.warning(
            f"{exclusion.bank} excluded at {fmt_term(exclusion.term_months)}: latest validated "
            f"observation is {exclusion.age_hours:.0f} h old "
            f"(limit {exclusion.threshold_hours:.0f} h)."
        )

    term = live_term_selector(overview, "overview_term")
    if term is None:
        return
    summary = unwrap(cached(services, "term_summary", term))
    if summary is None:
        return
    top = st.columns(4)
    top[0].metric("Market leader", ", ".join(summary.leaders))
    top[1].metric("Lowest", fmt_rate_pct(summary.lowest_pct))
    top[2].metric("Median", fmt_rate_pct(summary.median_pct))
    top[3].metric("Highest", fmt_rate_pct(summary.highest_pct))
    bottom = st.columns(3)
    bottom[0].metric("Spread", f"{summary.spread_bps:.0f} bps")
    bottom[1].metric("Banks observed", summary.banks_observed)
    bottom[2].metric("Fresh banks", summary.fresh_banks)
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Rank": r.rank,
                    "Bank": r.bank,
                    "Rate": fmt_rate_pct(r.rate_pct),
                    "Product": r.product_name,
                }
                for r in summary.ranking
            ]
        ),
        hide_index=True,
        width="stretch",
    )
