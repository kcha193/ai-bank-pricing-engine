"""Rate Movements: what changed recently, and what did it do to the ranking?

Timeline and event feed first, a single-event inspector beside them, and the full table on
demand. Events come from ``DashboardServices.changes`` (suspicious readings are excluded and
shown in Operations & Trust). A collection failure is never an event: when a bank could not be
collected the page says so above the timeline.

One product at a time: mortgage and deposit events are read separately (the product is part of
every service call and cache key), each event carries its own stored product type, and the
event inspector looks the event up in that product's history only.
"""

from __future__ import annotations

from collections import Counter

import pandas as pd
import streamlit as st

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard import theme, ui
from balance_pricing_engine.dashboard.charts import movement_timeline
from balance_pricing_engine.dashboard.formatting import (
    EM_DASH,
    fmt_bps,
    fmt_nz_datetime,
    fmt_rate_pct,
    fmt_term,
    http_url_or_none,
)
from balance_pricing_engine.dashboard.models import ChangeFilters, ChangeWindow, EventDetail
from balance_pricing_engine.dashboard.products import profile
from balance_pricing_engine.dashboard.views._common import cached, unwrap
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.pricing_intelligence.changes import ChangeType
from balance_pricing_engine.pricing_intelligence.health import CollectorStatus, FreshnessState

ALL = "All"
WINDOWS = {"Today": "today", "7 days": "7d", "30 days": "30d", "Custom": "custom"}
DIRECTIONS = {ALL: ALL, "UP": "▲ Increases", "DOWN": "▼ Decreases"}
FEED_LIMIT = 8


def _rank(value) -> str:
    return EM_DASH if value is None else f"#{value}"


def detail_rows(detail: EventDetail) -> list[tuple[str, str]]:
    """Event provenance as (field, value) text pairs.

    Rendered in a dataframe, where every cell is literal text: bank pages are untrusted, and
    scraped strings must never be interpreted as Markdown (links, images, directives).
    """
    return [
        ("Market", profile(detail.row.product_type).label),
        ("Product", f"{detail.row.bank} — {detail.product_name}"),
        ("Observed", fmt_nz_datetime(detail.observed_at)),
        ("Effective date", detail.effective_date_text or "not published"),
        ("Rate type", detail.rate_category),
        ("Rank", f"{_rank(detail.row.previous_rank)} → {_rank(detail.row.new_rank)}"),
        ("Market median at event", fmt_rate_pct(detail.market_median_pct)),
        ("Market state at event", detail.market_state or EM_DASH),
        ("Parser version", detail.parser_version or EM_DASH),
        ("Source", detail.source_url or EM_DASH),
    ]


def _table(rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Time": fmt_nz_datetime(r.detected_at),
                "Market": profile(r.product_type).label,
                "Bank": r.bank,
                "Term": fmt_term(r.term_months),
                "Rate type": r.rate_category,
                "Old": fmt_rate_pct(r.previous_pct),
                "New": fmt_rate_pct(r.current_pct),
                "Movement": fmt_bps(r.change_bps),
                "Prev rank": r.previous_rank if r.previous_rank is not None else EM_DASH,
                "New rank": r.new_rank if r.new_rank is not None else EM_DASH,
                "Type": r.change_type,
            }
            for r in rows
        ]
    )


def _filters(services: DashboardServices, product: ProductType):
    """Render the filter bar. Returns (window, filters), or None when the window is invalid.

    Every option here comes from configuration and is the same for both products, so the
    filters are kept when the product switches; only the event list itself is product-scoped.
    """
    config = services.settings.intelligence
    with ui.filter_bar():
        top = st.columns([2, 1, 1], vertical_alignment="bottom")
        label = top[0].radio(
            "Window", list(WINDOWS), index=1, horizontal=True, key="rate_changes_window"
        )
        start = end = None
        if WINDOWS[label] == "custom":
            start = top[1].date_input("From", key="rate_changes_from", format="DD/MM/YYYY")
            end = top[2].date_input("To", key="rate_changes_to", format="DD/MM/YYYY")
        flt = st.columns(4)
        bank = flt[0].selectbox("Bank", [ALL, *config.selected_banks], key="rate_changes_bank")
        term_labels = {fmt_term(t): t for t in config.tracked_terms_months}
        term = flt[1].selectbox(
            f"{profile(product).noun.capitalize()} term",
            [ALL, *term_labels],
            key="rate_changes_term",
        )
        direction = flt[2].selectbox(
            "Direction", list(DIRECTIONS), format_func=DIRECTIONS.get, key="rate_changes_direction"
        )
        change_type = flt[3].selectbox(
            "Event type", [ALL, *(t.value for t in ChangeType)], key="rate_changes_type"
        )
    if start is not None and end is not None and end < start:
        st.warning("The end date is before the start date; choose a window that runs forwards.")
        return None
    window = ChangeWindow(WINDOWS[label], start, end)  # type: ignore[arg-type]
    filters = ChangeFilters(
        bank=None if bank == ALL else bank,
        term_months=None if term == ALL else term_labels[term],
        direction=None if direction == ALL else direction,
        change_type=None if change_type == ALL else change_type,
    )
    return window, filters


def _coverage_notice(services: DashboardServices, product: ProductType) -> None:
    """Name banks we could not see, so a quiet feed is not mistaken for a quiet market."""
    health = services.health(product).value  # evaluated now, never cached
    if health is None:
        return
    failing = [b.bank for b in health.banks if b.collector is CollectorStatus.FAILING]
    stale = [
        b.bank
        for b in health.banks
        if b.freshness.state is not FreshnessState.FRESH and b.bank not in failing
    ]
    parts = []
    if failing:
        parts.append(f"collection currently failing for {', '.join(failing)}")
    if stale:
        parts.append(f"no fresh observation for {', '.join(stale)}")
    if parts:
        st.info(
            f"{profile(product).label} coverage is incomplete: "
            + "; ".join(parts)
            + ". Missing observations are not treated as repricing or withdrawals."
        )


def _summary(rows) -> None:
    counts = Counter(theme.change_group(r.change_type) for r in rows)
    cols = st.columns(len(theme.DIRECTION_SYMBOLS))
    for col, group in zip(cols, theme.DIRECTION_SYMBOLS):
        col.metric(f"{theme.DIRECTION_SYMBOLS[group]} {group}", counts.get(group, 0), border=True)


def _inspector(services: DashboardServices, rows, product: ProductType) -> None:
    ui.section("Event inspector", "Provenance for one event, exactly as stored.")
    labels = [
        f"{n}. {fmt_nz_datetime(r.detected_at)} · {r.bank} · "
        f"{fmt_term(r.term_months)} · {fmt_bps(r.change_bps)}"
        for n, r in enumerate(rows, start=1)
    ]
    index = labels.index(
        st.selectbox("Event", labels, key=ui.product_key("rate_changes_event", product))
    )
    row = rows[index]
    with st.container(border=True):
        left, right = st.columns(2)
        left.metric("Previous rate", fmt_rate_pct(row.previous_pct))
        right.metric("New rate", fmt_rate_pct(row.current_pct))
        left.metric("Movement", fmt_bps(row.change_bps))
        right.metric("Rank", f"{_rank(row.previous_rank)} → {_rank(row.new_rank)}")
    detail = unwrap(cached(services, "event_detail", row.key, row.product_type))
    if detail is None:
        return
    st.dataframe(
        pd.DataFrame(detail_rows(detail), columns=["Field", "Value"]),
        hide_index=True,
        width="stretch",
    )
    link = http_url_or_none(detail.source_url)
    if link:
        st.link_button("Open official source page", link, icon=":material/open_in_new:")


def render(services: DashboardServices) -> None:
    head = st.container()  # the title names the product, chosen just below it
    product = ui.product_selector("rate_changes_product")
    info = profile(product)
    with head:
        ui.page_header(
            info.title("Rate Movements"),
            f"Confirmed competitor {info.noun} repricing: when it happened, how far rates "
            "moved and what it did to the ranking.",
        )
    ui.product_banner(product)
    st.caption(
        f"Rises and cuts are not labelled good or bad: a {info.rate_noun} cut is "
        f"{'better for borrowers' if info.best_word == 'lowest' else 'worse for depositors'}"
        f" and changes the competitive picture for every bank. {info.trade_off}"
    )
    chosen = _filters(services, product)
    if chosen is None:
        return
    _coverage_notice(services, product)
    rows = unwrap(cached(services, "changes", *chosen, product))
    if rows is None:
        return

    _summary(rows)
    ui.section(
        f"{info.market} repricing timeline",
        "One lane per bank; marker size shows the size of the move. Order in time does not "
        "imply that one move caused another.",
    )
    st.altair_chart(
        movement_timeline(rows, services.settings.intelligence.selected_banks), width="stretch"
    )

    feed, inspector = st.columns([3, 2], gap="large")
    with feed:
        ui.section(f"{info.market} event feed", f"{len(rows)} {info.noun} events, newest first.")
        ui.movement_feed(rows, limit=FEED_LIMIT)
        if len(rows) > FEED_LIMIT:
            st.caption(f"Showing {FEED_LIMIT} of {len(rows)}; the full table is below.")
    with inspector:
        _inspector(services, rows, product)

    with st.expander("Advanced: full event table", expanded=False):
        st.dataframe(_table(rows), hide_index=True, width="stretch")
