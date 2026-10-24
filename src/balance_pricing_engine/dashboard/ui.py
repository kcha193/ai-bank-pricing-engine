"""Reusable presentation components for the Pricing Watch console.

Components render view-model values only: they format, they never compute market statistics.
Text from bank pages or error messages (product names, source URLs, parser versions, last
errors) never passes through a component here -- it belongs in dataframe cells -- and
``tests/unit/test_dashboard_boundaries.py`` scans this module like a page.

Shared filters: the selected product, term and highlighted bank follow the user between pages.
Each page keeps its own widget key (so a page's widgets are independent and testable), seeded
from a canonical session value when the page is opened and writing back to it on every run.
Pages scope their term and bank widget keys by product, so a term chosen for mortgages is only
carried over to deposits when the deposit market actually offers it.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

import streamlit as st

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard import theme
from balance_pricing_engine.dashboard.formatting import (
    EM_DASH,
    fmt_bps,
    fmt_nz_datetime,
    fmt_rate_pct,
    fmt_term,
)
from balance_pricing_engine.dashboard.models import ChangeRow
from balance_pricing_engine.dashboard.products import PRODUCT_ORDER, ProductProfile, profile

SHARED_TERM = "pw_shared_term"
SHARED_BANK = "pw_shared_bank"
SHARED_PRODUCT = "pw_shared_product"
#: ``?product=term_deposit`` opens a market page on deposits (bookmarks, links from alerts).
PRODUCT_QUERY_PARAM = "product"
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


# ----------------------------------------------------------------------------- product


def _product_from(value) -> Optional[ProductType]:
    try:
        return ProductType(value) if value else None
    except ValueError:
        return None


def product_key(base: str, product: ProductType) -> str:
    """A widget key scoped to one product. Home loans keep the original, unsuffixed key so the
    pre-dual-product pages (and anything keyed on them) behave exactly as before."""
    product = ProductType(product)
    return base if product is ProductType.HOME_LOAN else f"{base}_{product.value}"


def current_product() -> ProductType:
    """The product the user last selected anywhere (home loans until they choose)."""
    return _product_from(st.session_state.get(SHARED_PRODUCT)) or ProductType.HOME_LOAN


def set_product(product: ProductType) -> None:
    st.session_state[SHARED_PRODUCT] = ProductType(product).value


def product_selector(key: str) -> ProductType:
    """The persistent [ ⌂ HOME LOANS ] [ ◎ TERM DEPOSITS ] control.

    Seeded, in order, from this page's own choice, the shared choice, a ``?product=`` query
    parameter, then home loans; always writes back to the shared choice, so the product follows
    the user across pages. ``required`` stops a click on the active segment clearing it.
    """
    values = [product.value for product in PRODUCT_ORDER]
    if st.session_state.get(key) not in values:
        seed = (
            _product_from(st.session_state.get(SHARED_PRODUCT))
            or _product_from(st.query_params.get(PRODUCT_QUERY_PARAM))
            or ProductType.HOME_LOAN
        )
        st.session_state[key] = seed.value
    with st.container(key=f"pw-product-switch-{key}"):
        chosen = st.segmented_control(
            "Product market",
            values,
            key=key,
            required=True,
            format_func=lambda value: f"{profile(value).icon} {profile(value).label.upper()}",
            help="Home loans (lending) and term deposits (deposit funding) are separate markets, "
            "ranked in opposite directions. Your choice follows you between pages.",
        )
    product = _product_from(chosen) or ProductType.HOME_LOAN
    set_product(product)
    return product


def product_banner(product: ProductType) -> None:
    """A strong, product-coloured strip under a page header naming the market and its rule."""
    info = profile(product)
    with st.container(key=f"pw-banner-{info.product_type.value}"):
        with st.container(horizontal=True, gap="small", vertical_alignment="center"):
            product_badge(product)
            st.caption(f"{info.side} · {info.ranking_rule} · comparable tier: {info.tier}")


def product_badge(product: ProductType) -> None:
    info = profile(product)
    st.badge(info.label, icon=info.icon, color=info.badge)


def product_nav_button(
    destination: str, label: str, product: ProductType, *, key: str, icon: Optional[str] = None
) -> None:
    """A button that opens ``destination`` already switched to ``product``."""
    if st.button(label, key=key, icon=icon, on_click=set_product, args=(product,)):
        page = st.session_state.get(_PAGES, {}).get(destination)
        if page is not None:
            st.switch_page(page)


def ranking_help(info: ProductProfile) -> str:
    return (
        f"{info.leader_label}. {info.ranking_rule}. Rankings come from the backend snapshot; "
        f"tied banks share a rank. {info.trade_off}"
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
    """One repricing event as a compact card. Every value is structured backend data.

    The product tag comes from the event's own stored product type, never from the page.
    """
    group = theme.change_group(row.change_type)
    info = profile(row.product_type)
    with st.container(border=True):
        top, rates, move = st.columns([3, 3, 2], vertical_alignment="center")
        with top:
            with st.container(horizontal=True, gap="small"):
                st.badge(change_label(row), color=theme.CHANGE_BADGE_COLOURS[group])
                st.badge(info.tag, color=info.badge)
            st.markdown(f"**{row.bank}** · {fmt_term(row.term_months)} {info.noun}")
        rates.markdown(f"{fmt_rate_pct(row.previous_pct)} → **{fmt_rate_pct(row.current_pct)}**")
        rates.caption(fmt_nz_datetime(row.detected_at))
        move.markdown(f"##### {fmt_bps(row.change_bps) if row.change_bps is not None else EM_DASH}")


def movement_feed(rows: Sequence[ChangeRow], *, limit: int) -> None:
    with st.container(key="pw-feed"):
        for row in list(rows)[:limit]:
            movement_card(row)
