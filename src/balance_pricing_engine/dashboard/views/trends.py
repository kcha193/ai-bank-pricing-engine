"""Market Trends: how have rates and the market structure moved over time?

Rate history (step lines: a rate holds until it changes), the market leader and median, the
dispersion on its own basis-point axis, the repricing timeline and the current term structure.
History and per-date market statistics come from ``DashboardServices.history``; nothing is
interpolated across a period a bank was not advertising.

The product switch picks the market. History is reconstructed per product (the deposit leader on
each date is the *highest* rate), and lending and deposit rates are never drawn on one chart.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from balance_pricing_engine.dashboard import ui
from balance_pricing_engine.dashboard.charts import (
    history_lines,
    market_dispersion,
    market_levels,
    repricing_timeline,
    term_structure,
)
from balance_pricing_engine.dashboard.formatting import fmt_bps, fmt_nz_datetime, fmt_term
from balance_pricing_engine.dashboard.products import profile
from balance_pricing_engine.dashboard.views._common import cached, live_term_selector, unwrap
from balance_pricing_engine.dashboard.services import DashboardServices

RANGES = {"30 days": 30, "90 days": 90, "1 year": 365, "All": None}


def render(services: DashboardServices) -> None:
    head = st.container()  # the title names the product, chosen just below it
    product = ui.product_selector("trends_product")
    info = profile(product)
    with head:
        ui.page_header(
            info.title("Market Trends"),
            f"Historical advertised {info.label.lower()} rates, the market leader and median, "
            "dispersion and the sequence of repricing — for one term at a time.",
        )
    ui.product_banner(product)
    overview = unwrap(cached(services, "overview", product))
    if overview is None:
        return
    config = services.settings.intelligence
    with ui.filter_bar():
        cols = st.columns([1, 2, 1, 1], vertical_alignment="bottom")
        term = live_term_selector(overview, ui.product_key("trends_term", product), container=cols[0])
        if term is None:
            return
        banks = cols[1].multiselect(
            "Banks", config.selected_banks, default=config.selected_banks, key="trends_banks"
        )
        days = RANGES[cols[2].selectbox("Range", list(RANGES), index=1, key="trends_range")]
        highlight = ui.bank_selector(
            config.selected_banks,
            "trends_bank",
            default=config.our_bank,
            allow_none=True,
            container=cols[3],
        )
        st.caption(
            f"Rate type: {info.tier} (the comparable tier). Dates are NZ calendar days."
        )

    data = unwrap(cached(services, "history", term, tuple(banks), days, product))
    if data is None:
        return
    days_covered = tuple(m.day for m in data.market)

    ui.section(
        f"{info.market} rate history · {fmt_term(term)}",
        "Step lines: a rate holds until the bank changes it. A break means the bank had no "
        "comparable rate on those dates.",
    )
    if data.points:
        st.altair_chart(
            history_lines(
                data.points, days=days_covered, selected_bank=highlight, banks=config.selected_banks
            ),
            width="stretch",
        )
    else:
        st.info("Select at least one bank to plot its rate history.")

    left, right = st.columns(2, gap="large")
    with left:
        ui.section(
            f"{info.market} leader and median",
            f"Computed per date across every bank observed then. The leader line is the "
            f"{info.best_word} comparable rate ({info.ranking_rule.lower()}).",
        )
        st.altair_chart(
            market_levels(data.market, f"Leader ({info.best_word} rate)"), width="stretch"
        )
    with right:
        ui.section(
            f"{info.market} dispersion",
            "Highest minus lowest comparable rate, in basis points.",
        )
        st.altair_chart(market_dispersion(data.market), width="stretch")

    ui.section(
        f"{info.market} repricing timeline",
        f"Recorded {info.noun} rate moves in this range. Order in time does not imply causation.",
    )
    if not data.timeline:
        ui.calm_empty_state(
            f"No {info.noun} rate moves recorded in this range",
            "Rates held steady for every bank at this term. Widen the range to see earlier moves.",
        )
    else:
        st.altair_chart(repricing_timeline(data.timeline, config.selected_banks), width="stretch")
        with st.expander("Timeline as a table", expanded=False):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Time": fmt_nz_datetime(e.detected_at),
                            "Market": info.label,
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

    ui.section(
        f"{info.market} term structure today",
        f"Current {info.tier} across terms for the selected banks (stale rates left out).",
    )
    if banks:
        st.altair_chart(term_structure(overview.matrix, banks, highlight), width="stretch")
