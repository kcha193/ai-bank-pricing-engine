"""Analytical and product-isolation regressions for the premium dashboard."""

import pytest
from dashboard_harness import frame_with, run_page

from balance_pricing_engine.competitor_rates.models import ProductType

pytest.importorskip("streamlit.testing.v1")


def test_health_exposes_deposit_freshness_separately_from_lending(dual_market_factory):
    market = dual_market_factory(stale_deposit_banks=("Westpac",))
    app = run_page("system_health", market.database, market.now)
    assert not app.exception
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Fresh banks"] == "5 / 5"
    assert metrics["Term deposit · Fresh banks"] == "4 / 5"
    assert metrics["Term deposit · Collectors failing"] == "1"


def test_trends_data_alternative_uses_only_the_selected_product(dual_market_factory):
    market = dual_market_factory()
    app = run_page("trends", market.database, market.now)
    app.segmented_control(key="trends_product").set_value(ProductType.TERM_DEPOSIT.value).run()
    assert not app.exception
    data = frame_with(app, "Advertised rate (%)")
    assert set(data["Market"]) == {"Term deposits"}
    assert data["Advertised rate (%)"].max() == 4.25
    assert 4.79 not in set(data["Advertised rate (%)"])
    app.multiselect(key="trends_banks").set_value(["ANZ"]).run()
    assert set(frame_with(app, "Advertised rate (%)")["Bank"]) == {"ANZ"}


def test_scenario_tradeoff_chart_preserves_each_objective_and_rate():
    from balance_pricing_engine.dashboard import charts
    from balance_pricing_engine.dashboard.models import ScenarioRowView

    rows = [
        ScenarioRowView("Match median", "MARKET_MEDIAN", 0.048, 0.049, -2_000_000),
        ScenarioRowView("Match leader", "MARKET_LEADER", 0.047, 0.048, -3_000_000),
    ]
    spec = charts.scenario_objectives(rows, {"Match median": "Median"}).to_dict()
    plotted = [row for dataset in spec["datasets"].values() for row in dataset]
    assert {(r["scenario"], r["objective"], r["rate"]) for r in plotted} == {
        ("Median", -2_000_000, 4.9),
        ("Match leader", -3_000_000, 4.8),
    }
    assert spec["encoding"]["x"]["scale"]["zero"] is True


def test_history_series_are_distinguishable_without_colour():
    from datetime import date

    from balance_pricing_engine.dashboard.charts import history_lines
    from balance_pricing_engine.dashboard.models import HistoryPoint

    spec = history_lines(
        [
            HistoryPoint(date(2026, 10, 1), "ANZ", 4.9),
            HistoryPoint(date(2026, 10, 1), "BNZ", 4.8),
        ]
    ).to_dict()
    assert spec["layer"][0]["encoding"]["strokeDash"]["field"] == "bank"
    assert spec["layer"][1]["encoding"]["shape"]["field"] == "bank"


def test_history_chart_allows_time_zoom_without_zooming_rate_axis():
    from datetime import date

    from balance_pricing_engine.dashboard.charts import history_lines
    from balance_pricing_engine.dashboard.models import HistoryPoint

    spec = history_lines([HistoryPoint(date(2026, 10, 1), "ANZ", 4.9)]).to_dict()
    assert any(p.get("select", {}).get("encodings") == ["x"] for p in spec.get("params", []))
