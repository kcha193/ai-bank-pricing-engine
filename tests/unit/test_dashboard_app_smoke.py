from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "src/balance_pricing_engine/dashboard/app.py"


def test_router_renders_overview_by_default_with_no_data(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICING_INTELLIGENCE_DB_PATH", str(tmp_path / "none.db"))
    monkeypatch.setenv("PRICING_WATCH_CONFIG", str(tmp_path / "missing.yaml"))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert any("NZ Retail Bank Pricing Watch" in t.value for t in app.title)
    assert any("No market data" in i.value for i in app.info)
    assert not (tmp_path / "none.db").exists()  # a page view never creates the database
