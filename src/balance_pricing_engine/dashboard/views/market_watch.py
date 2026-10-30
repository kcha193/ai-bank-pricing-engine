"""Market Explorer: where does each bank sit, at this term and across terms?

Ranking, gap analysis, the bank x term matrix and the term structure all read the same backend
snapshot (``bank_positions``, ``term_summary``, ``overview.matrix``), so the charts and tables
always show identical comparable observations. Stale rates never enter a ranking; quarantined
fallbacks are labelled wherever they appear.

The product switch picks the market. Each product has its own comparable tier (advertised fixed
specials for mortgages; standard term deposits for deposits, with PIE returns, managed funds and
KiwiSaver excluded by the backend) and its own ranking direction -- every read below passes the
product, and every label names it.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard import ui
from balance_pricing_engine.dashboard.charts import gap_bars, position_dot_plot, term_structure
from balance_pricing_engine.dashboard.formatting import (
    fmt_bps,
    fmt_freshness,
    fmt_nz_datetime,
    fmt_rate_pct,
    fmt_term,
)
from balance_pricing_engine.dashboard.models import ChangeFilters, ChangeWindow
from balance_pricing_engine.dashboard.products import profile
from balance_pricing_engine.dashboard.views._common import (
    cached,
    live_term_selector,
    matrix_frame,
    matrix_legend,
    styled_matrix,
    unwrap,
)
from balance_pricing_engine.dashboard.services import DashboardServices


def _freshness(position) -> str:
    return (
        "⚠ QUARANTINED (last confirmed rate)"
        if position.quarantined
        else fmt_freshness(position.freshness)
    )


def _positions_frame(positions) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Bank": p.bank,
                "Rank": p.rank,
                "Rate": fmt_rate_pct(p.rate_pct),
                "vs leader": fmt_bps(p.vs_leader_bps),
                "vs median": fmt_bps(p.vs_median_bps),
                "Last change": (
                    fmt_nz_datetime(p.last_change_at) if p.last_change_at else "No recent change"
                ),
                "Freshness": _freshness(p),
            }
            for p in positions
        ]
    )


def _bank_card(selected, bank_count: int, product: ProductType) -> None:
    info = profile(product)
    cols = ui.metric_columns(5, key="bank-position")
    cols[0].metric(f"Current {info.rate_noun}", fmt_rate_pct(selected.rate_pct), border=True)
    cols[1].metric(
        "Rank",
        f"#{selected.rank}",
        border=True,
        help=f"Out of {bank_count} fresh banks. {ui.ranking_help(info)}",
    )
    cols[2].metric(
        "vs leader",
        fmt_bps(selected.vs_leader_bps),
        border=True,
        help=f"Basis points above (+) or below (−) the {info.best_word} comparable rate. "
        f"For {info.customer}, {info.better} is more attractive.",
    )
    cols[3].metric(
        "vs median",
        fmt_bps(selected.vs_median_bps),
        border=True,
        help="Basis points above (+) or below (−) the market median.",
    )
    cols[4].metric(
        "Freshness",
        "⚠ QUARANTINED" if selected.quarantined else fmt_freshness(selected.freshness),
        border=True,
    )


def _bank_detail(services: DashboardServices, overview, bank: str, product: ProductType) -> None:
    info = profile(product)
    ui.section(
        f"{bank} {info.label.lower()} in detail",
        f"{info.tier.capitalize()} across every tracked term, and this bank's recent "
        f"{info.noun} changes.",
    )
    row = matrix_frame(overview.matrix)
    if bank in row.index:
        st.dataframe(styled_matrix(row.loc[[bank]]), width="stretch")
    moves = cached(services, "changes", ChangeWindow("30d"), ChangeFilters(bank=bank), product)
    if moves.status == "ERROR":
        st.error(moves.message)
    elif not moves.value:
        st.caption(f"No confirmed {info.noun} changes for {bank} in the last 30 days.")
    else:
        ui.movement_feed(moves.value, limit=4)
    ui.nav_link(
        ui.MARKET_TRENDS, f"{info.market} rate history for {bank}", icon=":material/show_chart:"
    )


def render(services: DashboardServices) -> None:
    head = st.container()  # the title names the product, chosen just below it
    product = ui.product_selector("market_watch_product")
    info = profile(product)
    with head:
        ui.page_header(
            info.title("Market Explorer"),
            f"Compare {info.noun} competitors at a term and across terms: ranking, gaps to the "
            "leader and median, the full bank × term matrix and the term structure.",
        )
    ui.product_banner(product)
    overview = unwrap(cached(services, "overview", product))
    if overview is None:
        return
    config = services.settings.intelligence
    positions = summary = bank = None
    with ui.filter_bar():
        left, right, note = st.columns([1, 1, 2], vertical_alignment="bottom")
        term = live_term_selector(
            overview, ui.product_key("market_watch_term", product), container=left
        )
        if term is not None:
            positions = unwrap(cached(services, "bank_positions", term, product))
            summary = unwrap(cached(services, "term_summary", term, product))
        if positions and summary is not None:
            bank = ui.bank_selector(
                [p.bank for p in positions],
                ui.product_key("market_watch_bank", product),
                default=config.our_bank or positions[0].bank,
                container=right,
            )
        note.caption(
            f"Product: {info.label.lower()} · comparable tier: {info.tier}. Stale observations "
            "are excluded; quarantined readings show the last confirmed rate."
        )
    if term is None:
        st.dataframe(styled_matrix(matrix_frame(overview.matrix)), width="stretch")
        st.caption(matrix_legend(product))
        return
    if not positions or summary is None:
        return

    selected = next(p for p in positions if p.bank == bank)
    _bank_card(selected, summary.banks_observed, product)

    ranking, gaps, matrix, structure = st.tabs(
        ["Ranking", "Gap analysis", "Bank × term matrix", "Term structure"]
    )
    with ranking:
        ui.section(
            f"{info.market} ranking at {fmt_term(term)}",
            f"{info.leader_label}: ◆ {', '.join(summary.leaders)} at "
            f"{fmt_rate_pct(summary.best_pct)}; median {fmt_rate_pct(summary.median_pct)} "
            f"(dashed). {info.ranking_rule}.",
        )
        st.caption(info.trade_off)
        with ui.chart_panel("ranking"):
            ui.render_chart(position_dot_plot(positions, summary.median_pct, bank))
            st.caption(
                "Rate axis is narrowed to show differences; the values are labelled directly."
            )
        st.dataframe(_positions_frame(positions), hide_index=True, width="stretch")
    with gaps:
        ui.section(
            f"{info.market} competitive gaps",
            f"Basis points from each bank's rate to the {info.best_word}-rate leader and to the "
            f"median, as the backend computes them. Every other bank sits "
            f"{'above' if info.best_word == 'lowest' else 'below'} the {info.best_word} rate, "
            f"so gaps to the leader are zero or "
            f"{'positive' if info.best_word == 'lowest' else 'negative'}.",
        )
        with ui.chart_panel("gaps"):
            ui.render_chart(gap_bars(positions, bank))
    with matrix:
        ui.section(
            f"{info.market} bank × term matrix",
            f"Every tracked {info.noun} term, including stale terms kept for audit.",
        )
        st.dataframe(styled_matrix(matrix_frame(overview.matrix)), width="stretch")
        st.caption(matrix_legend(product))
        for exclusion in overview.exclusions:
            st.caption(
                f"⚠ {exclusion.bank} at {fmt_term(exclusion.term_months)}: "
                f"{exclusion.age_hours:.0f} h old (limit {exclusion.threshold_hours:.0f} h); "
                "excluded from market statistics."
            )
    with structure:
        ui.section(
            f"{info.market} term structure",
            f"{info.tier.capitalize()} across terms. Stale rates are left out; hollow points "
            "are quarantined fallbacks. Gaps mean the bank has no comparable rate at that term.",
        )
        chosen = st.multiselect(
            "Banks to compare",
            list(overview.matrix.banks),
            default=list(overview.matrix.banks),
            key=ui.product_key("explorer_structure_banks", product),
        )
        if chosen:
            ui.render_chart(term_structure(overview.matrix, chosen, bank))
        else:
            st.caption("Select at least one bank to draw its term structure.")

    _bank_detail(services, overview, bank, product)
