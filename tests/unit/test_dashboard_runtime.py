"""Exercise the real optional Streamlit renderer against local fixture stores."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
ROOT = Path(__file__).resolve().parents[2]


def render(database):
    config = yaml.safe_load((ROOT / "configs/base.yaml").read_text())
    config["pricing_intelligence"] = {"database_path": str(database)}
    config_yaml = yaml.safe_dump(config)
    # Substitute only the config file read. The actual app, widgets, service,
    # store, data frames and chart renderer all execute normally.
    script = f"""
from pathlib import Path
from unittest.mock import patch
import runpy
read_text = Path.read_text
def read_config(path, *args, **kwargs):
    if path == Path({str(ROOT / "configs/base.yaml")!r}):
        return {config_yaml!r}
    return read_text(path, *args, **kwargs)
with patch.object(Path, 'read_text', read_config):
    runpy.run_path({str(ROOT / "src/balance_pricing_engine/dashboard/app.py")!r})
"""
    return AppTest.from_string(script, default_timeout=30).run()


def seed(database, fixture_fetcher, *, age_days=1, fail_latest=False):
    config = PricingIntelligenceConfig(
        database_path=str(database),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    now = datetime.now(timezone.utc)
    with PricingIntelligenceService(
        config=config, store=ObservationStore(database), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=now - timedelta(days=age_days), persist_snapshot=False)
        if fail_latest:
            service._fetcher = fixture_fetcher(failing_banks=["ANZ"])
            service.collect(captured_at_utc=now, persist_snapshot=False)


def test_stale_store_shows_freshness_problem_instead_of_empty(tmp_path, fixture_fetcher):
    database = tmp_path / "stale.db"
    seed(database, fixture_fetcher, age_days=20)
    app = render(database)
    assert not app.exception
    assert any("fresh" in warning.value.lower() for warning in app.warning)
    assert not any("store is empty" in item.value for item in app.info)
    assert any("Last collection attempt" in caption.value for caption in app.caption)


def test_failed_bank_is_visible_while_last_known_rates_remain(tmp_path, fixture_fetcher):
    database = tmp_path / "partial.db"
    seed(database, fixture_fetcher, fail_latest=True)
    app = render(database)
    assert not app.exception
    assert "ANZ" in app.dataframe[0].value.index
    assert any("ANZ" in warning.value for warning in app.warning)
    assert any("Collection freshness" in item.value for item in app.subheader)
    app.selectbox[0].set_value(6).run()
    assert not app.exception


def test_unreadable_store_does_not_disable_scenario_controls(tmp_path):
    database = tmp_path / "invalid.db"
    database.write_text("not a SQLite database")
    app = render(database)
    assert not app.exception
    assert any("market data" in item.value.lower() for item in app.error)
    assert app.button[0].label == "Run scenario"
