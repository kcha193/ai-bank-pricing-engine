"""Plumbing shared by pages: cached reads, result rendering, header."""

from __future__ import annotations

from typing import Optional, TypeVar

import pandas as pd
import streamlit as st

from balance_pricing_engine.dashboard import ui
from balance_pricing_engine.dashboard.formatting import (
    EM_DASH,
    fmt_nz_datetime,
    fmt_rate_pct,
    fmt_term,
)
from balance_pricing_engine.dashboard.models import MarketMatrix, Overview
from balance_pricing_engine.dashboard.results import Result
from balance_pricing_engine.dashboard.services import DashboardServices

T = TypeVar("T")
CACHE_TTL_SECONDS = 60


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _cached(_services: DashboardServices, method: str, db_stamp: tuple, args: tuple) -> Result:
    return getattr(_services, method)(*args)


def cached(services: DashboardServices, method: str, *args) -> Result:
    """Cached read of stored state, for up to 60 s (and until the database file changes).

    Some of these views depend on the current time (e.g. "changes today"), so they can lag by up
    to a minute. Health and freshness are never routed through here: they are evaluated on every
    render. "Refresh Dashboard" clears this cache.
    """
    return _cached(services, method, services.db_stamp(), args)


def unwrap(result: Result[T]) -> Optional[T]:
    """Render an EMPTY/ERROR result as a message and return None; else the value."""
    if result.status == "ERROR":
        st.error(result.message)
        return None
    if result.status == "EMPTY":
        st.info(result.message)
        return None
    return result.value


def live_term_selector(
    overview: Overview, key: str, *, label: str = "Term", container=None
) -> Optional[int]:
    """Render live terms, retaining a valid choice or resetting to the service default.

    The choice is shared across pages (see ``ui.shared_selectbox``): picking 2Y on the Command
    Centre opens Market Explorer on 2Y too.
    """
    terms = overview.selectable_terms
    if not terms:
        st.session_state.pop(key, None)
        st.info(
            "No fresh comparable market terms are available. "
            "Stale observations are kept for audit in the Market Explorer matrix; "
            "collect rates through Operations & Trust."
        )
        return None
    labels = [fmt_term(term) for term in terms]
    chosen = ui.shared_selectbox(
        label,
        labels,
        key,
        shared_key=ui.SHARED_TERM,
        default=fmt_term(overview.default_term),
        container=container,
        help="Mortgage term to compare. Only terms with fresh, comparable rates are offered.",
    )
    return terms[labels.index(chosen)]


def matrix_frame(matrix: MarketMatrix) -> pd.DataFrame:
    """Bank x term table of display strings. Markers are text, never colour alone."""
    rows = {}
    for bank in matrix.banks:
        row = {}
        for term in matrix.terms:
            cell = matrix.cells.get((bank, term))
            if cell is None or cell.rate_pct is None:
                text = EM_DASH
            elif cell.stale:
                text = f"⚠ {fmt_rate_pct(cell.rate_pct)} (stale)"
            elif cell.quarantined:
                text = f"⚠ {fmt_rate_pct(cell.rate_pct)} (quarantined: last confirmed)"
            elif cell.is_leader:
                text = f"◆ {fmt_rate_pct(cell.rate_pct)}"
            else:
                text = fmt_rate_pct(cell.rate_pct)
            row[fmt_term(term)] = text
        rows[bank] = row
    frame = pd.DataFrame.from_dict(rows, orient="index")
    frame.index.name = "Bank"
    return frame


def exclusion_messages(exclusions) -> list[str]:
    """One sentence per stale bank (not per bank and term), in configured exclusion order."""
    by_bank: dict[str, list] = {}
    for exclusion in exclusions:
        by_bank.setdefault(exclusion.bank, []).append(exclusion)
    messages = []
    for bank, items in by_bank.items():
        ordered = sorted(items, key=lambda e: e.term_months)
        terms = ", ".join(fmt_term(e.term_months) for e in ordered)
        ages = sorted({round(e.age_hours) for e in items})
        age = f"{ages[0]} h" if len(ages) == 1 else f"{ages[0]}–{ages[-1]} h"
        limit = items[0].threshold_hours
        messages.append(
            f"{bank} excluded at {terms}: latest validated observation is {age} old "
            f"(limit {limit:.0f} h)."
        )
    return messages


MATRIX_LEGEND = (
    "◆ term leader (shaded teal) · ⚠ stale or quarantined (shaded amber; stale rates are "
    "excluded from rankings and statistics) · — not available"
)


def _cell_style(text: str) -> str:
    if text.startswith("⚠"):
        return "background-color: rgba(199, 124, 2, 0.16); font-style: italic;"
    if text.startswith("◆"):
        return "background-color: rgba(14, 140, 140, 0.16); font-weight: 700;"
    return ""


def styled_matrix(frame: pd.DataFrame):
    """Conditional shading for ``matrix_frame``. The markers stay in the text, so shading
    is never the only signal (and the underlying values are unchanged)."""
    return frame.style.map(_cell_style)


def render_header(services: DashboardServices) -> None:
    """Brand bar on every page: identity, data timestamp, activity and coverage."""
    result = services.header()  # uncached: freshness depends on 'now'
    header = result.value
    with st.container(key="pw-brand"):
        identity, status = st.columns([1, 2], vertical_alignment="center")
        with identity:
            st.title("NZ Retail Bank Pricing Watch", anchor=False)
            st.caption("Home-loan pricing intelligence · decision support only")
        with status:
            if header is None:
                st.caption(result.message)
                return
            with st.container(horizontal=True, horizontal_alignment="right", gap="small"):
                ui.market_state_badge(header.market_state)
                ui.coverage_badge(header.fresh_banks, header.configured_banks)
            with st.container(horizontal=True, horizontal_alignment="right", gap="medium"):
                st.caption(
                    f"Last successful update: {fmt_nz_datetime(header.last_update)} "
                    "(Pacific/Auckland)"
                )
                st.caption(f"Market activity: {header.market_state or EM_DASH}")
                st.caption(
                    f"Banks with fresh data: {header.fresh_banks} / {header.configured_banks}"
                )
