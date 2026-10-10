"""Rate Changes: what changed recently, and what did it do to the ranking?"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from balance_pricing_engine.dashboard.formatting import (
    EM_DASH,
    fmt_bps,
    fmt_nz_datetime,
    fmt_rate_pct,
    fmt_term,
)
from balance_pricing_engine.dashboard.models import ChangeFilters, ChangeWindow
from balance_pricing_engine.dashboard.pages._common import cached, unwrap
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.pricing_intelligence.changes import ChangeType

ALL = "All"
WINDOWS = {"Today": "today", "7 days": "7d", "30 days": "30d", "Custom": "custom"}


def _rank(value) -> str:
    return EM_DASH if value is None else f"#{value}"


def render(services: DashboardServices) -> None:
    config = services.settings.intelligence
    top = st.columns(4)
    label = top[0].radio(
        "Window", list(WINDOWS), index=1, horizontal=True, key="rate_changes_window"
    )
    start = end = None
    if WINDOWS[label] == "custom":
        start = top[1].date_input("From", key="rate_changes_from")
        end = top[2].date_input("To", key="rate_changes_to")
    window = ChangeWindow(WINDOWS[label], start, end)  # type: ignore[arg-type]

    flt = st.columns(4)
    bank = flt[0].selectbox("Bank", [ALL, *config.selected_banks], key="rate_changes_bank")
    term_labels = {fmt_term(t): t for t in config.tracked_terms_months}
    term = flt[1].selectbox("Term", [ALL, *term_labels], key="rate_changes_term")
    direction = flt[2].selectbox("Direction", [ALL, "UP", "DOWN"], key="rate_changes_direction")
    change_type = flt[3].selectbox(
        "Event type", [ALL, *(t.value for t in ChangeType)], key="rate_changes_type"
    )
    filters = ChangeFilters(
        bank=None if bank == ALL else bank,
        term_months=None if term == ALL else term_labels[term],
        direction=None if direction == ALL else direction,
        change_type=None if change_type == ALL else change_type,
    )

    rows = unwrap(cached(services, "changes", window, filters))
    if rows is None:
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Time": fmt_nz_datetime(r.detected_at),
                    "Bank": r.bank,
                    "Term": fmt_term(r.term_months),
                    "Old": fmt_rate_pct(r.previous_pct),
                    "New": fmt_rate_pct(r.current_pct),
                    "Movement": fmt_bps(r.change_bps),
                    "Prev rank": r.previous_rank if r.previous_rank is not None else EM_DASH,
                    "New rank": r.new_rank if r.new_rank is not None else EM_DASH,
                    "Type": r.change_type,
                }
                for r in rows
            ]
        ),
        hide_index=True,
        width="stretch",
    )

    labels = [
        f"{n}. {fmt_nz_datetime(r.detected_at)} · {r.bank} · "
        f"{fmt_term(r.term_months)} · {fmt_bps(r.change_bps)}"
        for n, r in enumerate(rows, start=1)
    ]
    index = labels.index(st.selectbox("Event detail", labels, key="rate_changes_event"))
    detail = unwrap(cached(services, "event_detail", rows[index].key))
    if detail is None:
        return
    source = (
        f"[official page]({detail.source_url})"
        if detail.source_url and detail.source_url.startswith(("http://", "https://"))
        else (detail.source_url or EM_DASH)
    )
    st.markdown(
        "\n".join(
            [
                f"- **Bank / product:** {detail.row.bank} — {detail.product_name}",
                f"- **Source:** {source}",
                f"- **Observed:** {fmt_nz_datetime(detail.observed_at)}",
                f"- **Effective date:** {detail.effective_date_text or 'not published'}",
                f"- **Rate type:** {detail.rate_category}",
                f"- **Rank:** {_rank(detail.row.previous_rank)} → {_rank(detail.row.new_rank)}",
                f"- **Market median at event:** {fmt_rate_pct(detail.market_median_pct)}",
                f"- **Market state at event:** {detail.market_state or EM_DASH}",
                f"- **Parser version:** {detail.parser_version or EM_DASH}",
            ]
        )
    )
