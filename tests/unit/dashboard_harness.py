"""Shared helpers for dashboard tests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings


def make_services(market) -> DashboardServices:
    """Services bound to a ``market_factory`` database and its fixed clock."""
    settings = load_settings({}, database_path=str(market.database))
    return DashboardServices(settings, clock=lambda: market.now)


def run_page(page: str, database: Path, now: datetime, *, setup: str = ""):
    """Run one dashboard page against a fixture database with ``AppTest``.

    ``setup`` is extra Python executed before the page renders -- used to patch the
    optimiser or operational actions so tests stay fast and never touch the network.
    """
    import pytest

    import streamlit as st

    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    st.cache_data.clear()  # results are cached per (db path, mtime); never leak across tests
    script = f"""
from datetime import datetime
{setup}
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.dashboard.pages import {page} as page

settings = load_settings({{}}, database_path={str(database)!r})
services = DashboardServices(settings, clock=lambda: datetime.fromisoformat({now.isoformat()!r}))
page.render(services)
"""
    return AppTest.from_string(script, default_timeout=30).run()
