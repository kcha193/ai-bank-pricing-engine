"""Shared helpers for dashboard tests."""

from __future__ import annotations

from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings


def make_services(market) -> DashboardServices:
    """Services bound to a ``market_factory`` database and its fixed clock."""
    settings = load_settings({}, database_path=str(market.database))
    return DashboardServices(settings, clock=lambda: market.now)
