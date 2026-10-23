"""Command Centre: what is the market doing, what changed, where do we sit, can we trust it?

The landing page answers five questions in that order -- market pulse, the selected term's
market story, recent repricing, factual insights and data trust -- then links onward. Every
figure is a ``DashboardServices`` value; the insight sentences are templated in ``insights``.
"""

from __future__ import annotations

import streamlit as st

from balance_pricing_engine.dashboard import ui
from balance_pricing_engine.dashboard.charts import position_dot_plot
from balance_pricing_engine.dashboard.formatting import fmt_bps, fmt_rate_pct, fmt_term
from balance_pricing_engine.dashboard.insights import build_insights
from balance_pricing_engine.dashboard.models import ChangeFilters, ChangeWindow
from balance_pricing_engine.dashboard.views._common import (
    MATRIX_LEGEND,
    cached,
    exclusion_messages,
    live_term_selector,
    matrix_frame,
    styled_matrix,
    unwrap,
)
from balance_pricing_engine.dashboard.services import DashboardServices

FEED_LIMIT = 5


def _exclusion_warnings(overview) -> None:
    for message in exclusion_messages(overview.exclusions):
        st.warning(message)


def _matrix(overview, *, expanded: bool) -> None:
    with st.expander("All terms at a glance — bank × term matrix", expanded=expanded):
        st.dataframe(styled_matrix(matrix_frame(overview.matrix)), width="stretch")
        st.caption(MATRIX_LEGEND)


def _quick_actions() -> None:
    ui.section("Next steps")
    with st.container(horizontal=True, gap="medium"):
        ui.nav_link(
            ui.MARKET_EXPLORER, "Explore competitor positioning", icon=":material/leaderboard:"
        )
        ui.nav_link(ui.RATE_MOVEMENTS, "Investigate rate changes", icon=":material/swap_vert:")
        ui.nav_link(ui.PRICING_STUDIO, "Open a pricing scenario", icon=":material/tune:")
        ui.nav_link(ui.OPERATIONS, "Review data quality", icon=":material/verified:")


def _pulse(overview, summary) -> None:
    header = overview.header
    cols = st.columns(6)
    cols[0].metric(
        "Market leader",
        ", ".join(summary.leaders),
        border=True,
        help="Bank(s) advertising the lowest comparable special at this term.",
    )
    cols[1].metric("Leader rate", fmt_rate_pct(summary.lowest_pct), border=True)
    cols[2].metric(
        "Market median",
        fmt_rate_pct(summary.median_pct),
        border=True,
        help="Median of fresh, comparable advertised specials at this term.",
    )
    cols[3].metric(
        "Dispersion",
        f"{summary.spread_bps:.0f} bps",
        border=True,
        help="Gap between the highest and lowest comparable rate at this term.",
    )
    cols[4].metric(
        "Repricing · 7d",
        overview.banks_repricing_7d,
        border=True,
        help="Banks with at least one confirmed comparable rate move in the last 7 days.",
    )
    cols[5].metric(
        "Fresh coverage",
        f"{header.fresh_banks} / {header.configured_banks}",
        border=True,
        help="Banks whose latest validated observation is within the freshness limit.",
    )
    st.caption(f"Market activity rationale: {overview.market_state_rationale}")


def _selected_panel(positions, summary, bank) -> None:
    selected = next((p for p in positions if p.bank == bank), None)
    with st.container(border=True):
        if selected is None:
            st.markdown("**Your position**")
            st.caption(
                "Choose a bank to highlight above to see its rank and gaps here."
                if bank is None
                else f"{bank} has no fresh comparable rate at this term."
            )
            st.metric("Highest", fmt_rate_pct(summary.highest_pct))
            st.metric("Banks observed", summary.banks_observed)
            return
        st.markdown(f"**{selected.bank} at {fmt_term(summary.term_months)}**")
        left, right = st.columns(2)
        left.metric("Rate", fmt_rate_pct(selected.rate_pct))
        right.metric("Rank", f"#{selected.rank} of {summary.banks_observed}")
        left.metric("Gap to leader", fmt_bps(selected.vs_leader_bps))
        right.metric("Gap to median", fmt_bps(selected.vs_median_bps))
        if selected.quarantined:
            st.caption("⚠ Latest reading quarantined; this is the last confirmed rate.")


def render(services: DashboardServices) -> None:
    ui.page_header(
        "Command Centre",
        "What the NZ home-loan market is doing, what changed, where your bank sits — "
        "and whether the data behind it can be trusted.",
    )
    overview = unwrap(cached(services, "overview"))
    if overview is None:
        ui.nav_link(ui.OPERATIONS, "Go to Operations & Trust", icon=":material/arrow_forward:")
        return

    config = services.settings.intelligence
    with ui.filter_bar():
        left, right, note = st.columns([1, 1, 2], vertical_alignment="bottom")
        term = live_term_selector(overview, "overview_term", container=left)
        bank = ui.bank_selector(
            config.selected_banks,
            "overview_bank",
            default=config.our_bank,
            allow_none=True,
            container=right,
        )
        note.caption("Comparable tier: advertised specials. Stale rates are excluded.")
    if term is None:
        _exclusion_warnings(overview)
        _matrix(overview, expanded=True)
        _quick_actions()
        return

    summary = unwrap(cached(services, "term_summary", term))
    positions = unwrap(cached(services, "bank_positions", term))
    if summary is None or positions is None:
        return

    ui.section("Market pulse", f"{fmt_term(term)} advertised specials, latest stored data.")
    _pulse(overview, summary)

    ui.section(
        "Market story",
        f"{summary.banks_observed} banks · range {fmt_rate_pct(summary.lowest_pct)}–"
        f"{fmt_rate_pct(summary.highest_pct)} · stems run from the dashed market median "
        "to each bank's rate.",
    )
    chart, panel = st.columns([5, 2])
    chart.altair_chart(position_dot_plot(positions, summary.median_pct, bank), width="stretch")
    with panel:
        _selected_panel(positions, summary, bank)

    feed, side = st.columns([3, 2], gap="large")
    with feed:
        ui.section("What changed", "Confirmed changes in the last 7 days, newest first.")
        changes = cached(services, "changes", ChangeWindow("7d"), ChangeFilters())
        if changes.status == "ERROR":
            st.error(changes.message)
        elif not changes.value:
            ui.calm_empty_state(
                "No confirmed repricing in the last 7 days",
                "Advertised rates held steady at every bank collected. Collection failures are "
                "tracked in Operations & Trust and are never shown here as withdrawals.",
            )
        else:
            ui.movement_feed(changes.value, limit=FEED_LIMIT)
            extra = len(changes.value) - FEED_LIMIT
            if extra > 0:
                st.caption(f"{extra} more in Rate Movements.")
        ui.nav_link(ui.RATE_MOVEMENTS, "All rate movements", icon=":material/arrow_forward:")
    with side:
        ui.section("Key insights")
        with st.container(key="pw-insights", border=True):
            st.markdown(
                "\n".join(
                    f"- {line}" for line in build_insights(overview, summary, positions, bank)
                )
            )
        ui.section("Data trust")
        _exclusion_warnings(overview)
        if not overview.exclusions:
            st.caption(
                f"✓ No stale rates are excluded at any term; "
                f"{overview.header.fresh_banks} of {overview.header.configured_banks} banks "
                "have fresh data."
            )
        ui.nav_link(
            ui.OPERATIONS, "Freshness and collector health", icon=":material/arrow_forward:"
        )

    _matrix(overview, expanded=False)
    _quick_actions()
