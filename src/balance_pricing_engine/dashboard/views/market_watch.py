"""Market Explorer: where does each bank sit, at this term and across terms?

Ranking, gap analysis, the bank x term matrix and the term structure all read the same backend
snapshot (``bank_positions``, ``term_summary``, ``overview.matrix``), so the charts and tables
always show identical comparable observations. Stale rates never enter a ranking; quarantined
fallbacks are labelled wherever they appear.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

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
from balance_pricing_engine.dashboard.views._common import (
    MATRIX_LEGEND,
    cached,
    live_term_selector,
    matrix_frame,
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


def _bank_card(selected, bank_count: int) -> None:
    cols = st.columns(5)
    cols[0].metric("Current", fmt_rate_pct(selected.rate_pct), border=True)
    cols[1].metric(
        "Rank", f"#{selected.rank}", border=True, help=f"Out of {bank_count} fresh banks."
    )
    cols[2].metric(
        "vs leader",
        fmt_bps(selected.vs_leader_bps),
        border=True,
        help="Basis points above (+) or below (−) the lowest comparable rate.",
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


def _bank_detail(services: DashboardServices, overview, bank: str) -> None:
    ui.section(
        f"{bank} in detail",
        "Advertised specials across every tracked term, and this bank's recent changes.",
    )
    row = matrix_frame(overview.matrix)
    if bank in row.index:
        st.dataframe(styled_matrix(row.loc[[bank]]), width="stretch")
    moves = cached(services, "changes", ChangeWindow("30d"), ChangeFilters(bank=bank))
    if moves.status == "ERROR":
        st.error(moves.message)
    elif not moves.value:
        st.caption(f"No confirmed changes for {bank} in the last 30 days.")
    else:
        ui.movement_feed(moves.value, limit=4)
    ui.nav_link(ui.MARKET_TRENDS, f"Rate history for {bank}", icon=":material/show_chart:")


def render(services: DashboardServices) -> None:
    ui.page_header(
        "Market Explorer",
        "Compare competitors at a term and across terms: ranking, gaps to the leader and "
        "median, the full bank × term matrix and the term structure.",
    )
    overview = unwrap(cached(services, "overview"))
    if overview is None:
        return
    config = services.settings.intelligence
    positions = summary = bank = None
    with ui.filter_bar():
        left, right, note = st.columns([1, 1, 2], vertical_alignment="bottom")
        term = live_term_selector(overview, "market_watch_term", container=left)
        if term is not None:
            positions = unwrap(cached(services, "bank_positions", term))
            summary = unwrap(cached(services, "term_summary", term))
        if positions and summary is not None:
            bank = ui.bank_selector(
                [p.bank for p in positions],
                "market_watch_bank",
                default=config.our_bank or positions[0].bank,
                container=right,
            )
        note.caption(
            "Product: home loans · comparable tier: advertised specials. Stale observations "
            "are excluded; quarantined readings show the last confirmed rate."
        )
    if term is None:
        st.dataframe(styled_matrix(matrix_frame(overview.matrix)), width="stretch")
        st.caption(MATRIX_LEGEND)
        return
    if not positions or summary is None:
        return

    selected = next(p for p in positions if p.bank == bank)
    _bank_card(selected, summary.banks_observed)

    ranking, gaps, matrix, structure = st.tabs(
        ["Ranking", "Gap analysis", "Bank × term matrix", "Term structure"]
    )
    with ranking:
        ui.section(
            f"Competitor ranking at {fmt_term(term)}",
            f"Leader {', '.join(summary.leaders)} at {fmt_rate_pct(summary.lowest_pct)}; "
            f"median {fmt_rate_pct(summary.median_pct)} (dashed). Lower is more competitive "
            "for a borrower.",
        )
        st.altair_chart(position_dot_plot(positions, summary.median_pct, bank), width="stretch")
        st.dataframe(_positions_frame(positions), hide_index=True, width="stretch")
    with gaps:
        ui.section(
            "Competitive gaps",
            "Basis points from each bank's rate to the market leader and to the median, "
            "as the backend computes them.",
        )
        st.altair_chart(gap_bars(positions, bank), width="stretch")
    with matrix:
        ui.section(
            "Bank × term matrix",
            "Every tracked term, including stale terms kept for audit.",
        )
        st.dataframe(styled_matrix(matrix_frame(overview.matrix)), width="stretch")
        st.caption(MATRIX_LEGEND)
        for exclusion in overview.exclusions:
            st.caption(
                f"⚠ {exclusion.bank} at {fmt_term(exclusion.term_months)}: "
                f"{exclusion.age_hours:.0f} h old (limit {exclusion.threshold_hours:.0f} h); "
                "excluded from market statistics."
            )
    with structure:
        ui.section(
            "Term structure",
            "Advertised specials across terms. Stale rates are left out; hollow points are "
            "quarantined fallbacks. Gaps mean the bank has no comparable rate at that term.",
        )
        chosen = st.multiselect(
            "Banks to compare",
            list(overview.matrix.banks),
            default=list(overview.matrix.banks),
            key="explorer_structure_banks",
        )
        if chosen:
            st.altair_chart(term_structure(overview.matrix, chosen, bank), width="stretch")
        else:
            st.caption("Select at least one bank to draw its term structure.")

    _bank_detail(services, overview, bank)
