"""Plumbing shared by pages: cached reads, result rendering, header."""

from __future__ import annotations

from typing import Optional, TypeVar

import pandas as pd
import streamlit as st

from balance_pricing_engine.dashboard.formatting import (
    EM_DASH,
    fmt_nz_datetime,
    fmt_rate_pct,
    fmt_term,
)
from balance_pricing_engine.dashboard.models import MarketMatrix
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


def render_header(services: DashboardServices) -> None:
    st.title("NZ Retail Bank Pricing Watch")
    result = services.header()  # uncached: freshness depends on 'now'
    header = result.value
    if header is None:
        st.caption(result.message)
        return
    cols = st.columns(3)
    cols[0].caption(
        f"Last successful update: {fmt_nz_datetime(header.last_update)} (Pacific/Auckland)"
    )
    cols[1].caption(f"Market activity: {header.market_state or EM_DASH}")
    cols[2].caption(f"Banks with fresh data: {header.fresh_banks} / {header.configured_banks}")
