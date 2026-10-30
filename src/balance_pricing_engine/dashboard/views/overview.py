"""Command Centre: both markets at a glance, then one market in depth.

Two overview panels come first -- the home-loan (lending) market and the term-deposit (funding)
market, each read from its own backend snapshot and ranked in its own direction. Below them a
product switch picks the market for the detailed story: positioning, recent repricing, factual
insights, data trust and the bank x term matrix. Every figure is a ``DashboardServices`` value;
the insight sentences are templated in ``insights``. A product with no data shows its own empty
state -- the other product's rates are never substituted.
"""

from __future__ import annotations

import streamlit as st

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard import ui
from balance_pricing_engine.dashboard.charts import position_dot_plot
from balance_pricing_engine.dashboard.formatting import (
    fmt_bps,
    fmt_nz_datetime,
    fmt_rate_pct,
    fmt_term,
)
from balance_pricing_engine.dashboard.insights import build_insights
from balance_pricing_engine.dashboard.models import ChangeFilters, ChangeWindow
from balance_pricing_engine.dashboard.products import PRODUCT_ORDER, profile
from balance_pricing_engine.dashboard.views._common import (
    cached,
    exclusion_messages,
    live_term_selector,
    matrix_frame,
    matrix_legend,
    styled_matrix,
    unwrap,
)
from balance_pricing_engine.dashboard.services import DashboardServices

FEED_LIMIT = 5


def _exclusion_warnings(overview) -> None:
    info = profile(overview.product_type)
    for message in exclusion_messages(overview.exclusions):
        st.warning(f"{info.symbol} {info.label}: {message}")


def _matrix(overview, *, expanded: bool) -> None:
    info = profile(overview.product_type)
    with st.expander(f"All {info.noun} terms at a glance — bank × term matrix", expanded=expanded):
        st.dataframe(styled_matrix(matrix_frame(overview.matrix)), width="stretch")
        st.caption(matrix_legend(overview.product_type))


def _quick_actions(product: ProductType) -> None:
    info = profile(product)
    ui.section("Next steps")
    with st.container(horizontal=True, gap="medium"):
        ui.nav_link(
            ui.MARKET_EXPLORER,
            f"Explore {info.noun} competitor positioning",
            icon=":material/leaderboard:",
        )
        ui.nav_link(
            ui.RATE_MOVEMENTS, f"Investigate {info.noun} rate changes", icon=":material/swap_vert:"
        )
        ui.nav_link(ui.PRICING_STUDIO, "Open a pricing scenario", icon=":material/tune:")
        ui.nav_link(ui.OPERATIONS, "Review data quality", icon=":material/verified:")


# --------------------------------------------------------------- market overview panels


def _panel_metrics(info, overview, summary) -> None:
    header = overview.header
    top = ui.metric_columns(3, key=f"headline-{info.product_type.value}")
    top[1].metric(
        f"{info.noun.capitalize()} leader",
        ", ".join(summary.leaders),
        border=True,
        help=ui.ranking_help(info),
    )
    top[0].metric(
        f"{info.best_word.capitalize()} rate",
        fmt_rate_pct(summary.best_pct),
        border=True,
        help=f"The {info.best_word} fresh comparable {info.rate_noun} at this term.",
    )
    top[2].metric(
        "Market median",
        fmt_rate_pct(summary.median_pct),
        border=True,
        help=f"Median of fresh, comparable {info.tier} at this term.",
    )
    bottom = ui.metric_columns(3, key=f"market-{info.product_type.value}")
    bottom[0].metric(
        "Competitive spread",
        f"{summary.spread_bps:.0f} bps",
        border=False,
        help="Gap between the highest and lowest comparable rate at this term.",
    )
    bottom[1].metric(
        "Repricing · 7d",
        overview.banks_repricing_7d,
        border=False,
        help=f"Banks with at least one confirmed comparable {info.noun} rate move in the last "
        "7 days.",
    )
    bottom[2].metric(
        "Fresh coverage",
        f"{header.fresh_banks} / {header.configured_banks}",
        border=False,
        help=f"Banks whose latest validated {info.noun} observation is within the freshness limit.",
    )


def _market_panel(services: DashboardServices, product: ProductType) -> None:
    """One product's headline market at its default term, or an honest empty state."""
    info = profile(product)
    with st.container(border=True, key=f"pw-panel-{product.value}"):
        st.subheader(f"{info.icon} {info.market} market", anchor=False)
        st.caption(f"{info.symbol} {info.side} · {info.ranking_rule}")
        result = cached(services, "overview", product)
        overview = result.value
        if overview is None:
            if result.status == "ERROR":
                st.error(result.message)
            else:
                ui.calm_empty_state(f"No {info.noun} market data yet", result.message)
            return
        term = overview.default_term
        summary = (
            cached(services, "term_summary", term, product).value if term is not None else None
        )
        if summary is None:
            st.warning(
                f"No fresh comparable {info.noun} rates: every {info.label.lower()} observation "
                "is stale, so no leader or median is shown. Stale rates are kept for audit in the "
                "Market Explorer matrix."
            )
        else:
            st.caption(f"{fmt_term(term)} comparable {info.rate_noun}s · latest stored data")
            _panel_metrics(info, overview, summary)
            if overview.exclusions:
                banks = sorted({e.bank for e in overview.exclusions})
                st.caption(f"⚠ Stale and excluded: {', '.join(banks)}.")
        st.caption(
            f"Last successful {info.noun} update: "
            f"{fmt_nz_datetime(overview.header.last_update)} (Pacific/Auckland)"
        )
        ui.product_nav_button(
            ui.MARKET_EXPLORER,
            f"Explore {info.label.lower()}",
            product,
            key=f"cc_explore_{product.value}",
            icon=info.icon,
        )


# ------------------------------------------------------------------ detailed analysis


def _selected_panel(positions, summary, bank) -> None:
    info = profile(summary.product_type)
    selected = next((p for p in positions if p.bank == bank), None)
    with st.container(border=True):
        if selected is None:
            st.markdown(f"**Your {info.noun} position**")
            st.caption(
                "Choose a bank to highlight above to see its rank and gaps here."
                if bank is None
                else f"{bank} has no fresh comparable {info.noun} rate at this term."
            )
            st.metric("Leader rate", fmt_rate_pct(summary.best_pct))
            st.metric("Banks observed", summary.banks_observed)
            return
        st.markdown(f"**{selected.bank} · {fmt_term(summary.term_months)} {info.noun}**")
        left, right = st.columns(2)
        left.metric("Rate", fmt_rate_pct(selected.rate_pct))
        right.metric(
            "Rank",
            f"#{selected.rank} of {summary.banks_observed}",
            help=ui.ranking_help(info),
        )
        left.metric(
            "Gap to leader",
            fmt_bps(selected.vs_leader_bps),
            help=f"This bank's rate minus the {info.best_word} comparable rate.",
        )
        right.metric("Gap to median", fmt_bps(selected.vs_median_bps))
        if selected.quarantined:
            st.caption("⚠ Latest reading quarantined; this is the last confirmed rate.")


def _feed(services: DashboardServices, product: ProductType) -> None:
    info = profile(product)
    ui.section(
        f"What changed · {info.label}",
        f"Confirmed {info.noun} changes in the last 7 days, newest first.",
    )
    changes = cached(services, "changes", ChangeWindow("7d"), ChangeFilters(), product)
    if changes.status == "ERROR":
        st.error(changes.message)
    elif not changes.value:
        ui.calm_empty_state(
            f"No confirmed {info.noun} repricing in the last 7 days",
            f"Advertised {info.label.lower()} rates held steady at every bank collected. "
            "Collection failures are tracked in Operations & Trust and are never shown here as "
            "withdrawals.",
        )
    else:
        ui.movement_feed(changes.value, limit=FEED_LIMIT)
        extra = len(changes.value) - FEED_LIMIT
        if extra > 0:
            st.caption(f"{extra} more in Rate Movements.")
    ui.product_nav_button(
        ui.RATE_MOVEMENTS,
        f"All {info.noun} rate movements",
        product,
        key=f"cc_movements_{product.value}",
        icon=":material/arrow_forward:",
    )


def _detail(services: DashboardServices, product: ProductType) -> None:
    info = profile(product)
    overview = unwrap(cached(services, "overview", product))
    if overview is None:
        return
    config = services.settings.intelligence
    with ui.filter_bar():
        left, right, note = st.columns([1, 1, 2], vertical_alignment="bottom")
        term = live_term_selector(
            overview, ui.product_key("overview_term", product), container=left
        )
        bank = ui.bank_selector(
            config.selected_banks,
            "overview_bank",
            default=config.our_bank,
            allow_none=True,
            container=right,
        )
        note.caption(f"Comparable tier: {info.tier}. Stale rates are excluded.")
    if term is None:
        _exclusion_warnings(overview)
        _matrix(overview, expanded=True)
        _quick_actions(product)
        return

    summary = unwrap(cached(services, "term_summary", term, product))
    positions = unwrap(cached(services, "bank_positions", term, product))
    if summary is None or positions is None:
        return

    ui.section(
        f"{info.market} competitor positioning · {fmt_term(term)}",
        f"{summary.banks_observed} banks · range {fmt_rate_pct(summary.lowest_pct)}–"
        f"{fmt_rate_pct(summary.highest_pct)} · leader ◆ {', '.join(summary.leaders)} at "
        f"{fmt_rate_pct(summary.best_pct)} ({info.best_word} rate) · stems run from the dashed "
        "market median to each bank's rate.",
    )
    st.caption(f"Market activity rationale: {overview.market_state_rationale}")
    with ui.chart_panel("positioning"):
        chart, panel = st.columns([5, 2])
        ui.render_chart(position_dot_plot(positions, summary.median_pct, bank), container=chart)
        with panel:
            _selected_panel(positions, summary, bank)

    feed, side = st.columns([3, 2], gap="large")
    with feed:
        _feed(services, product)
    with side:
        ui.section(f"Key insights · {info.label}")
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
                f"✓ No stale {info.noun} rates are excluded at any term; "
                f"{overview.header.fresh_banks} of {overview.header.configured_banks} banks "
                "have fresh data."
            )
        ui.nav_link(
            ui.OPERATIONS, "Freshness and collector health", icon=":material/arrow_forward:"
        )

    _matrix(overview, expanded=False)
    _quick_actions(product)


def render(services: DashboardServices) -> None:
    ui.page_header(
        "Command Centre",
        "Your view of NZ lending and deposit funding. Compare competitors, track repricing "
        "and check the evidence behind every number.",
    )
    with st.container(key="pw-markets"):
        columns = st.columns(len(PRODUCT_ORDER), gap="medium")
        for column, product in zip(columns, PRODUCT_ORDER):
            with column:
                _market_panel(services, product)

    ui.section(
        "Detailed market analysis",
        "Switch between the two markets. Mortgage and deposit figures are never combined.",
    )
    product = ui.product_selector("overview_product")
    ui.product_banner(product)
    _detail(services, product)
