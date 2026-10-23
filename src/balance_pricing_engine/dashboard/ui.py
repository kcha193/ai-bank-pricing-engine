"""Reusable presentation components for the Pricing Watch console.

Components render view-model values only: they format, they never compute market statistics.
Text from bank pages or error messages (product names, source URLs, parser versions, last
errors) never passes through a component here -- it belongs in dataframe cells -- and
``tests/unit/test_dashboard_boundaries.py`` scans this module like a page.

Shared filters: the selected term and the highlighted bank follow the user between pages.
Each page keeps its own widget key (so a page's widgets are independent and testable), seeded
from a canonical session value when the page is opened and writing back to it on every run.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

import streamlit as st

from balance_pricing_engine.dashboard import theme
from balance_pricing_engine.dashboard.formatting import (
    EM_DASH,
    fmt_bps,
    fmt_nz_datetime,
    fmt_rate_pct,
    fmt_term,
)
from balance_pricing_engine.dashboard.models import ChangeRow

SHARED_TERM = "pw_shared_term"
SHARED_BANK = "pw_shared_bank"
_PAGES = "pw_registered_pages"

#: Destination ids used for cross-page links. ``app.py`` registers the matching pages.
COMMAND_CENTRE = "command_centre"
MARKET_EXPLORER = "market_explorer"
RATE_MOVEMENTS = "rate_movements"
MARKET_TRENDS = "market_trends"
PRICING_STUDIO = "pricing_studio"
OPERATIONS = "operations"


# ------------------------------------------------------------------------------ shell


def inject_theme() -> None:
    """Scoped CSS for the whole app (a style-only block takes no layout space)."""
    st.html(theme.APP_CSS)


def register_pages(pages: dict) -> None:
    """Called by the router each run so components can link to other destinations."""
    st.session_state[_PAGES] = pages


def nav_link(destination: str, label: str, *, icon: Optional[str] = None, help=None) -> None:
    """A link to another destination. Pages rendered on their own (tests, embeds) skip it."""
    page = st.session_state.get(_PAGES, {}).get(destination)
    if page is None:
        return
    st.page_link(page, label=label, icon=icon, help=help)


def page_header(title: str, subtitle: str) -> None:
    with st.container(key="pw-page-header"):
        st.header(title, anchor=False)
        st.caption(subtitle)


def section(title: str, caption: Optional[str] = None) -> None:
    st.subheader(title, anchor=False)
    if caption:
        st.caption(caption)


@contextmanager
def filter_bar() -> Iterator[None]:
    """One tinted strip holding a page's filters, so they read as a unit."""
    with st.container(key="pw-filters"):
        yield


def calm_empty_state(title: str, body: str) -> None:
    """A quiet, explanatory empty state (for 'nothing happened', not for errors)."""
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.caption(body)


def market_state_badge(state: Optional[str]) -> None:
    if not state:
        return
    colour, meaning = theme.MARKET_STATE_STYLE.get(state, ("gray", state.title()))
    st.badge(meaning, icon=":material/insights:", color=colour)


def coverage_badge(fresh: int, configured: int) -> None:
    if configured and fresh == configured:
        st.badge(f"All {configured} banks fresh", icon=":material/check_circle:", color="green")
    else:
        st.badge(
            f"{fresh} of {configured} banks fresh", icon=":material/warning:", color="orange"
        )


# ---------------------------------------------------------------------- shared filters


def shared_selectbox(
    label: str,
    options: Sequence[str],
    key: str,
    *,
    shared_key: str,
    default: Optional[str],
    allow_none: bool = False,
    placeholder: Optional[str] = None,
    container=None,
    help: Optional[str] = None,
) -> Optional[str]:
    """A selectbox whose value follows the user across pages.

    Keeps this page's own valid choice; otherwise starts from the shared choice if it is
    still offered, else ``default``. An option that disappears (a term that went stale, say)
    is dropped rather than shown.
    """
    options = list(options)
    has_own = key in st.session_state
    current = st.session_state.get(key)
    if not (has_own and (current in options or (allow_none and current is None))):
        shared = st.session_state.get(shared_key)
        seed = shared if shared in options else default
        if seed not in options and not allow_none:
            seed = options[0] if options else None
        st.session_state[key] = seed
    chosen = (container or st).selectbox(
        label,
        options,
        index=None if allow_none else 0,
        key=key,
        placeholder=placeholder,
        help=help,
    )
    st.session_state[shared_key] = chosen
    return chosen


def bank_selector(
    banks: Sequence[str],
    key: str,
    *,
    default: Optional[str] = None,
    allow_none: bool = False,
    label: str = "Highlight bank",
    container=None,
) -> Optional[str]:
    return shared_selectbox(
        label,
        banks,
        key,
        shared_key=SHARED_BANK,
        default=default,
        allow_none=allow_none,
        placeholder="Choose a bank to highlight",
        container=container,
        help="The bank to emphasise in charts and gap measures. It follows you between pages.",
    )


# --------------------------------------------------------------------------- feed


def change_label(row: ChangeRow) -> str:
    group = theme.change_group(row.change_type)
    return f"{theme.DIRECTION_SYMBOLS[group]} {group}"


def movement_card(row: ChangeRow) -> None:
    """One repricing event as a compact card. Every value is structured backend data."""
    group = theme.change_group(row.change_type)
    with st.container(border=True):
        top, rates, move = st.columns([3, 3, 2], vertical_alignment="center")
        with top:
            st.badge(change_label(row), color=theme.CHANGE_BADGE_COLOURS[group])
            st.markdown(f"**{row.bank}** · {fmt_term(row.term_months)}")
        rates.markdown(f"{fmt_rate_pct(row.previous_pct)} → **{fmt_rate_pct(row.current_pct)}**")
        rates.caption(fmt_nz_datetime(row.detected_at))
        move.markdown(f"##### {fmt_bps(row.change_bps) if row.change_bps is not None else EM_DASH}")


def movement_feed(rows: Sequence[ChangeRow], *, limit: int) -> None:
    with st.container(key="pw-feed"):
        for row in list(rows)[:limit]:
            movement_card(row)
