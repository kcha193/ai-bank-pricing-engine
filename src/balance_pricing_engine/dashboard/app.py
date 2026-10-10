"""Pricing Watch dashboard entry point.

Launch: ``PYTHONPATH=src streamlit run src/balance_pricing_engine/dashboard/app.py``
(``pip install -e ".[dashboard]"`` makes the PYTHONPATH prefix unnecessary).
"""

from __future__ import annotations

import streamlit as st

from balance_pricing_engine.dashboard.pages import (
    market_watch,
    operations,
    overview,
    rate_changes,
    scenarios,
    system_health,
    trends,
)
from balance_pricing_engine.dashboard.pages._common import render_header
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings


def main() -> None:
    st.set_page_config(page_title="NZ Pricing Watch", layout="wide")
    services = DashboardServices(load_settings())

    def page(module, title: str, path: str, default: bool = False):
        return st.Page(lambda: module.render(services), title=title, url_path=path, default=default)

    navigation = st.navigation(
        [
            page(overview, "Overview", "overview", True),
            page(market_watch, "Market Watch", "market-watch"),
            page(rate_changes, "Rate Changes", "rate-changes"),
            page(trends, "Historical Trends", "trends"),
            page(scenarios, "Pricing Scenarios", "scenarios"),
            page(operations, "Operations", "operations"),
            page(system_health, "System Health", "system-health"),
        ]
    )
    render_header(services)
    navigation.run()


if __name__ == "__main__":
    main()
