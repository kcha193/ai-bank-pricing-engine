from datetime import datetime, timezone

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest


def render_header(database, now):
    script = f"""
from datetime import datetime
from balance_pricing_engine.dashboard.pages._common import render_header
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings

settings = load_settings({{}}, database_path={str(database)!r})
render_header(DashboardServices(settings, clock=lambda: datetime.fromisoformat({now.isoformat()!r})))
"""
    return AppTest.from_string(script, default_timeout=30).run()


def captions(app):
    return [c.value for c in app.caption]


def test_header_shows_title_last_update_in_nz_time_market_activity_and_fresh_banks(market_factory):
    market = market_factory()
    app = render_header(market.database, market.now)
    assert not app.exception
    assert any("NZ Retail Bank Pricing Watch" in t.value for t in app.title)
    text = captions(app)
    # run-2 completed 2 minutes after 10 Oct 00:00 UTC = 13:02 NZDT
    assert "Last successful update: 10 Oct 2026 13:02 (Pacific/Auckland)" in text
    assert any(c.startswith("Market activity: ") for c in text)
    assert "Banks with fresh data: 5 / 5" in text


def test_header_counts_only_banks_whose_data_is_fresh(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    assert "Banks with fresh data: 4 / 5" in captions(render_header(market.database, market.now))


def test_header_degrades_to_the_guidance_message_without_a_database(tmp_path):
    app = render_header(tmp_path / "none.db", datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not app.exception
    assert any("No market data" in c for c in captions(app))
