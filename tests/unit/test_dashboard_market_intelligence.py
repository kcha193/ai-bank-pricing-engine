"""The dashboard's market-intelligence section, against a populated store."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

# Collection timestamps are dated relative to "now", not pinned to a calendar
# date. Market snapshots exclude observations older than
# max_observation_age_hours (168h by default), so a fixed date here passes when
# written and starts failing silently once real time crosses that window --
# which is exactly what happened on 2026-08-27.
#
# Offsets are whole days (not hours) so DAY_ONE and DAY_TWO always land on
# distinct calendar dates regardless of what UTC hour the suite runs at --
# get_rate_history_series() dedupes observations by captured_at_utc.date(),
# so same-day anchors would collapse the two-point history the tests exercise.
DAY_ONE = datetime.now(timezone.utc) - timedelta(days=2)
DAY_TWO = datetime.now(timezone.utc) - timedelta(days=1)


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

    # A plain importlib.import_module() would raise DashboardStop from inside
    # the module body and, per CPython's import machinery, remove the
    # partially-initialised module from sys.modules on that failure -- losing
    # access to module-level helpers (e.g. _resolve_intelligence_config) that
    # were already defined before the point of failure. Executing manually
    # keeps the module object (and everything defined up to the exception)
    # reachable via fake.module regardless.
    spec = importlib.util.find_spec("balance_pricing_engine.dashboard.app")
    module = importlib.util.module_from_spec(spec)
    sys.modules["balance_pricing_engine.dashboard.app"] = module
    with pytest.raises(DashboardStop):
        spec.loader.exec_module(module)
    fake.module = module
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


def test_history_chart_is_not_blanked_by_a_different_products_withdrawal(
    monkeypatch, tmp_path, make_record
):
    """A bank withdrawing its "standard" product must not blank the "special"
    line's still-valid chart history: the dashboard charts rate_category=
    "special" only, so the withdrawal markers passed to build_history_series
    must be scoped the same way, not just to bank+term."""
    from balance_pricing_engine.competitor_rates.models import ProductType
    from balance_pricing_engine.pricing_intelligence.changes import ChangeType, RateChange

    later_day = DAY_ONE + timedelta(days=2)
    path = tmp_path / "observations.db"
    config = PricingIntelligenceConfig(database_path=str(path))
    with PricingIntelligenceService(config=config, store=ObservationStore(path)) as service:
        service.store.record_observations(
            [
                make_record(bank="ANZ", rate_category="special", rate_pct=4.79),
                make_record(bank="BNZ", rate_category="special", rate_pct=4.85),
            ],
            observed_at=DAY_ONE,
        )
        # A later BNZ reprice introduces a chart date after ANZ's (unrelated)
        # standard-product withdrawal, so the bug's ffill-through-withdrawal
        # would otherwise blank ANZ's still-current special rate here too.
        service.store.record_observations(
            [make_record(bank="BNZ", rate_category="special", rate_pct=4.75)],
            observed_at=later_day,
        )
        service.store.record_changes(
            [
                RateChange(
                    observation_key="ANZ|home_loan|standard|12|unknown|fixed standard",
                    bank="ANZ",
                    product_type=ProductType.HOME_LOAN,
                    product_name="Fixed Standard",
                    rate_category="standard",
                    term_months=12,
                    change_type=ChangeType.REMOVED_PRODUCT,
                    detected_at=DAY_TWO,
                    run_id="test-run",
                )
            ]
        )

    fake = render_dashboard(monkeypatch, str(path))

    chart = fake.charts[0]
    assert chart.loc[later_day.date(), "ANZ"] == 4.79


def test_section_guides_the_user_when_no_data_exists(monkeypatch, tmp_path):
    """The suggested recovery command must actually work: every tracked
    bank's collector now needs --render-javascript (collectors/base.py:
    "Every tracked bank now builds its rate table client-side"), so a plain
    `collect` invocation returns a ParserError for every bank and leaves the
    dashboard exactly as empty as before -- the guidance must show the
    browser-rendering flag and its install prerequisite, not the static
    HTTP command that cannot succeed."""
    fake = render_dashboard(monkeypatch, str(tmp_path / "missing.db"))
    messages = " ".join(fake.texts("info"))

    assert "No market data collected yet" in messages
    assert "pricing_intelligence collect --render-javascript" in messages
    assert "playwright" in messages


def _minimal_engine_yaml(**pricing_intelligence_overrides):
    return yaml.dump(
        {
            "random_seed": 1,
            "simulation": {"n_loan_customers": 100, "n_deposit_customers": 100},
            "market": {
                "cash_rate": 0.04,
                "wholesale_funding_spread": 0.01,
                "competitor_loan_rate": 0.06,
                "competitor_deposit_rate": 0.04,
                "home_loan_reference_rate": 0.0525,
                "term_deposit_reference_rate": 0.0400,
            },
            "pricing_strategy": {
                "home_loan_spread_bps": {"min": -50, "max": 50, "step": 5},
                "term_deposit_spread_bps": {"min": -40, "max": 40, "step": 5},
            },
            "balance_sheet": {
                "required_deposit_funding_ratio": 0.65,
                "funding_shortfall_penalty_rate": 0.02,
            },
            "response": {
                "loan_rate_elasticity": -85.0,
                "loan_competitor_gap_elasticity": -65.0,
                "deposit_rate_elasticity": 95.0,
                "deposit_competitor_gap_elasticity": 75.0,
            },
            "pricing_intelligence": pricing_intelligence_overrides,
        }
    )


def test_resolve_intelligence_config_pulls_settings_from_the_sidebar_yaml(
    monkeypatch, tmp_path
):
    """The sidebar's pricing_intelligence block must not be silently ignored."""
    fake = render_dashboard(monkeypatch, str(tmp_path / "missing.db"))

    resolved = fake.module._resolve_intelligence_config(
        _minimal_engine_yaml(selected_banks=["ANZ", "ASB"])
    )

    assert resolved.selected_banks == ["ANZ", "ASB"]


def test_resolve_intelligence_config_falls_back_to_defaults_on_invalid_yaml(
    monkeypatch, tmp_path
):
    """The panel renders before the scenario gate, so the YAML may be mid-edit."""
    fake = render_dashboard(monkeypatch, str(tmp_path / "missing.db"))

    resolved = fake.module._resolve_intelligence_config("not: valid: engine: config: [")

    assert resolved == PricingIntelligenceConfig()
