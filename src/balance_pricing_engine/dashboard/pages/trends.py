"""Historical Trends: how have rates and the market moved?"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from balance_pricing_engine.dashboard.charts import history_lines, market_trend, repricing_timeline
from balance_pricing_engine.dashboard.formatting import fmt_bps, fmt_nz_datetime, fmt_term
from balance_pricing_engine.dashboard.pages._common import cached, unwrap
from balance_pricing_engine.dashboard.services import DashboardServices

RANGES = {"30 days": 30, "90 days": 90, "1 year": 365, "All": None}
MARKET_SERIES = {"Median": "median", "Leader": "leader", "Spread": "spread"}


def render(services: DashboardServices) -> None:
    overview = unwrap(cached(services, "overview"))
    if overview is None or not overview.matrix.terms:
        return
    config = services.settings.intelligence
    terms = list(overview.matrix.terms)
    labels = [fmt_term(t) for t in terms]
    cols = st.columns(3)
    chosen = cols[0].selectbox(
        "Term",
        labels,
        index=terms.index(overview.default_term) if overview.default_term in terms else 0,
        key="trends_term",
    )
    term = terms[labels.index(chosen)]
    banks = cols[1].multiselect(
        "Banks", config.selected_banks, default=config.selected_banks, key="trends_banks"
    )
    days = RANGES[cols[2].selectbox("Range", list(RANGES), index=1, key="trends_range")]
    st.caption("Rate type: advertised specials (the comparable tier).")

    data = unwrap(cached(services, "history", term, tuple(banks), days))
    if data is None:
        return

    st.subheader("Advertised rates")
    if data.points:
        st.altair_chart(history_lines(data.points), width="stretch")
    else:
        st.info("Select at least one bank to plot its rate history.")

    st.subheader("Market over time")
    series = MARKET_SERIES[
        st.radio("Series", list(MARKET_SERIES), horizontal=True, key="trends_market_series")
    ]
    st.altair_chart(market_trend(data.market, series), width="stretch")

    st.subheader("Repricing timeline")
    if not data.timeline:
        st.info("No rate moves recorded in this range.")
        return
    st.altair_chart(repricing_timeline(data.timeline), width="stretch")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Time": fmt_nz_datetime(e.detected_at),
                    "Bank": e.bank,
                    "Movement": fmt_bps(e.change_bps),
                    "Days after first move": e.offset_days,
                }
                for e in data.timeline
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption("Recorded sequence of rate moves; order does not imply causation.")
