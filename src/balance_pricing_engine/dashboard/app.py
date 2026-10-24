"""Pricing Watch dashboard entry point.

Launch: ``PYTHONPATH=src streamlit run src/balance_pricing_engine/dashboard/app.py``
(``pip install -e ".[dashboard]"`` makes the PYTHONPATH prefix unnecessary).

Six destinations in a top navigation bar. Every pre-redesign URL (``/overview``,
``/market-watch``, ``/rate-changes``, ``/trends``, ``/scenarios``, ``/system-health``) is a hidden
page that redirects to its new home, so old bookmarks keep working.

The page modules live in ``views/``, not ``pages/``: Streamlit treats a ``pages/`` folder next to
the main script as a legacy multipage app until ``st.navigation`` first runs, so a deep link
opened before that (e.g. right after a restart) rendered a bare module or "Page not found".
"""

from __future__ import annotations

import streamlit as st

from balance_pricing_engine.dashboard import ui
from balance_pricing_engine.dashboard.views import (
    market_watch,
    operations_trust,
    overview,
    rate_changes,
    scenarios,
    trends,
)
from balance_pricing_engine.dashboard.views._common import render_header
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings

#: (destination id, module, title, url path, icon). The first entry is the landing page.
DESTINATIONS = (
    (ui.COMMAND_CENTRE, overview, "Command Centre", "command-centre", ":material/dashboard:"),
    (ui.MARKET_EXPLORER, market_watch, "Market Explorer", "market-explorer", ":material/leaderboard:"),
    (ui.RATE_MOVEMENTS, rate_changes, "Rate Movements", "rate-movements", ":material/swap_vert:"),
    (ui.MARKET_TRENDS, trends, "Market Trends", "market-trends", ":material/show_chart:"),
    (ui.PRICING_STUDIO, scenarios, "Pricing Studio", "pricing-studio", ":material/tune:"),
    (ui.OPERATIONS, operations_trust, "Operations & Trust", "operations", ":material/verified:"),
)

#: Pre-redesign URL path -> the destination that replaced it.
LEGACY_PATHS = {
    "overview": ui.COMMAND_CENTRE,
    "market-watch": ui.MARKET_EXPLORER,
    "rate-changes": ui.RATE_MOVEMENTS,
    "trends": ui.MARKET_TRENDS,
    "scenarios": ui.PRICING_STUDIO,
    "system-health": ui.OPERATIONS,  # now the "Data health" tab
}


def main() -> None:
    st.set_page_config(
        page_title="NZ Pricing Watch",
        page_icon=":material/monitoring:",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    ui.inject_theme()
    services = DashboardServices(load_settings())

    pages = {
        dest: st.Page(
            lambda module=module: module.render(services),
            title=title,
            url_path=path,
            icon=icon,
            default=index == 0,
        )
        for index, (dest, module, title, path, icon) in enumerate(DESTINATIONS)
    }
    redirects = [
        st.Page(
            lambda target=pages[dest]: st.switch_page(target),
            title=f"Moved: /{path}",
            url_path=path,
            visibility="hidden",
        )
        for path, dest in LEGACY_PATHS.items()
    ]
    ui.register_pages(pages)
    navigation = st.navigation([*pages.values(), *redirects], position="top")
    render_header(services)
    navigation.run()


if __name__ == "__main__":
    main()
