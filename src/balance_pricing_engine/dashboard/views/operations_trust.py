"""Operations & Trust: can the data be trusted, and how is it collected?

Composes two existing pages without changing either: ``system_health`` (read-only data health)
and ``operations`` (the only page that can start a collection, behind its own token gate,
confirmation, single-flight runner and cooldowns). Only this composer imports
``operations``; nothing here can reach ``dashboard/actions.py`` except through it.
"""

from __future__ import annotations

import streamlit as st

from balance_pricing_engine.dashboard import ui
from balance_pricing_engine.dashboard.views import operations, system_health
from balance_pricing_engine.dashboard.services import DashboardServices


def render(services: DashboardServices) -> None:
    ui.page_header(
        "Operations & Trust",
        "Data freshness, collector health, quarantined readings and run history — and the "
        "controls that collect new market data.",
    )
    health_tab, controls_tab = st.tabs(["Data health", "Collection & maintenance"])
    with health_tab:
        system_health.render(services)
    with controls_tab:
        operations.render(services)
