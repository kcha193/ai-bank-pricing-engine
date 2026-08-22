"""The dashboard's market-intelligence section, against a populated store."""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

DAY_ONE = datetime(2026, 8, 20, tzinfo=timezone.utc)
DAY_TWO = DAY_ONE + timedelta(days=1)


class FakeContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class DashboardStop(Exception):
    """Raised in place of st.stop()."""


class FakeStreamlit(FakeContext):
    """Streamlit double covering everything the market-intelligence section uses."""

    def __init__(self):
        self.sidebar = FakeContext()
        self.calls: list[tuple] = []
        self.frames: list = []
        self.charts: list = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def __getattr__(self, name):
        def recorder(*args, **kwargs):
            self._record(name, *args, **kwargs)

        return recorder

    def set_page_config(self, **kwargs):
        self._record("set_page_config", **kwargs)

    def text_area(self, *args, **kwargs):
        self._record("text_area", *args, **kwargs)
        return "random_seed: 1\n"

    def checkbox(self, *args, **kwargs):
        self._record("checkbox", *args, **kwargs)
        return False

    def button(self, *args, **kwargs):
        self._record("button", *args, **kwargs)
        return False

    def selectbox(self, label, options, index=0, **kwargs):
        self._record("selectbox", label, options, index)
        return list(options)[index]

    def dataframe(self, frame, **kwargs):
        self._record("dataframe", frame)
        self.frames.append(frame)

    def line_chart(self, data, **kwargs):
        self._record("line_chart", data)
        self.charts.append(data)

    def columns(self, count):
        self._record("columns", count)
        return [self for _ in range(count)]

    def tabs(self, labels):
        self._record("tabs", labels)
        return [self for _ in labels]

    def cache_data(self, **kwargs):
        def decorator(func):
            return func

        return decorator

    def stop(self):
        raise DashboardStop()

    def texts(self, name):
        return [args[0] for call, args, _ in self.calls if call == name and args]


@pytest.fixture
def populated_store(tmp_path, fixture_fetcher):
    path = tmp_path / "observations.db"
    config = PricingIntelligenceConfig(
        database_path=str(path),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(path), fetcher=fixture_fetcher()
    ) as service:
        service.collect(captured_at_utc=DAY_ONE, persist_snapshot=False)
        service._fetcher = fixture_fetcher(
            mutate=lambda bank, html: html.replace(
                "<td>1 year</td><td>4.79%", "<td>1 year</td><td>4.59%"
            )
            if bank == "ANZ"
            else html
        )
        service.collect(captured_at_utc=DAY_TWO, persist_snapshot=False)
    return str(path)


def render_dashboard(monkeypatch, database_path):
    fake = FakeStreamlit()
    monkeypatch.setitem(sys.modules, "streamlit", fake)

    # The dashboard builds its own default config; point it at the test store.
    original_init = PricingIntelligenceConfig.__init__

    def patched_init(self, **kwargs):
        kwargs.setdefault("database_path", database_path)
        original_init(self, **kwargs)

    monkeypatch.setattr(PricingIntelligenceConfig, "__init__", patched_init)
    sys.modules.pop("balance_pricing_engine.dashboard.app", None)
    with pytest.raises(DashboardStop):
        importlib.import_module("balance_pricing_engine.dashboard.app")
    return fake


def test_section_renders_the_market_grid_and_summary(monkeypatch, populated_store):
    fake = render_dashboard(monkeypatch, populated_store)

    assert "NZ market intelligence" in fake.texts("header")
    subheaders = fake.texts("subheader")
    assert "Current mortgage market (advertised specials)" in subheaders
    assert "Recent repricing" in subheaders
    assert "Rate history" in subheaders
    assert any(header.startswith("Market state:") for header in subheaders)


def test_section_renders_market_metrics(monkeypatch, populated_store):
    fake = render_dashboard(monkeypatch, populated_store)
    metric_labels = [args[0] for call, args, _ in fake.calls if call == "metric"]

    assert {"Best", "Median", "Highest", "Spread"} <= set(metric_labels)


def test_section_renders_grid_ranking_and_changes_frames(monkeypatch, populated_store):
    fake = render_dashboard(monkeypatch, populated_store)

    assert len(fake.frames) >= 3
    grid = fake.frames[0]
    assert "1Y" in grid.columns
    assert "Median" in grid.index

    changes = fake.frames[-1]
    assert "Move (bps)" in changes.columns
    assert -20.0 in list(changes["Move (bps)"])


def test_section_charts_rate_history(monkeypatch, populated_store):
    fake = render_dashboard(monkeypatch, populated_store)

    assert fake.charts, "history should be charted once two collections exist"
    assert "ANZ" in fake.charts[0].columns


def test_section_guides_the_user_when_no_data_exists(monkeypatch, tmp_path):
    fake = render_dashboard(monkeypatch, str(tmp_path / "missing.db"))
    messages = " ".join(fake.texts("info"))

    assert "No market data collected yet" in messages
    assert "pricing_intelligence collect" in messages
