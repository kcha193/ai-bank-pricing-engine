from balance_pricing_engine.dashboard.charts import position_dot_plot
from balance_pricing_engine.dashboard.models import BankPosition
from balance_pricing_engine.pricing_intelligence.health import FreshnessState


def _pos(bank, rate, rank):
    return BankPosition(bank, rate, rank, 0.0, 0.0, None, FreshnessState.FRESH)


def _points(chart):
    spec = chart.to_dict()
    return [row for rows in spec["datasets"].values() for row in rows]


def test_dot_plot_has_a_point_per_bank_a_median_rule_and_flags_leader_and_selection():
    chart = position_dot_plot(
        [_pos("ANZ", 4.79, 1), _pos("ASB", 4.89, 4)], median_pct=4.85, selected_bank="ASB"
    )
    points = _points(chart)
    roles = {p["bank"]: p["role"] for p in points if "bank" in p}
    assert roles == {"ANZ": "Leader", "ASB": "Selected"}
    assert [p for p in points if p.get("rate") == 4.85 and "bank" not in p], "median rule missing"


def test_dot_plot_marks_unselected_non_leaders_as_other():
    chart = position_dot_plot(
        [_pos("ANZ", 4.79, 1), _pos("BNZ", 4.85, 2)], 4.82, selected_bank=None
    )
    roles = {p["bank"]: p["role"] for p in _points(chart) if "bank" in p}
    assert roles == {"ANZ": "Leader", "BNZ": "Other"}


def test_history_lines_is_one_chart_with_a_bank_colour_channel():
    from datetime import date

    from balance_pricing_engine.dashboard.charts import history_lines
    from balance_pricing_engine.dashboard.models import HistoryPoint

    spec = history_lines(
        [HistoryPoint(date(2026, 10, 1), "ANZ", 4.9), HistoryPoint(date(2026, 10, 1), "BNZ", 4.85)]
    ).to_dict()
    assert spec["encoding"]["color"]["field"] == "bank"
    assert len(next(iter(spec["datasets"].values()))) == 2


def test_market_trend_selects_the_requested_series():
    from datetime import date

    from balance_pricing_engine.dashboard.charts import market_trend
    from balance_pricing_engine.dashboard.models import MarketPoint

    market = [MarketPoint(date(2026, 10, 1), 4.85, 4.79, 10.0, ("ANZ",))]
    for series, expected in (("median", 4.85), ("leader", 4.79), ("spread", 10.0)):
        rows = next(iter(market_trend(market, series).to_dict()["datasets"].values()))
        assert rows[0]["value"] == expected


def test_repricing_timeline_plots_one_dot_per_event():
    from datetime import datetime, timezone

    from balance_pricing_engine.dashboard.charts import repricing_timeline
    from balance_pricing_engine.dashboard.models import TimelineEvent

    when = datetime(2026, 10, 10, tzinfo=timezone.utc)
    spec = repricing_timeline(
        [TimelineEvent(when, "ANZ", -10.0, 0.0), TimelineEvent(when, "BNZ", -10.0, 2.0)]
    ).to_dict()
    assert {r["bank"] for r in next(iter(spec["datasets"].values()))} == {"ANZ", "BNZ"}


def test_objective_curve_plots_the_curve_in_percent_and_annotates_each_marker_once():
    from balance_pricing_engine.dashboard.charts import objective_curve
    from balance_pricing_engine.dashboard.models import CurvePoint

    spec = objective_curve(
        [CurvePoint(0.047, -3.0), CurvePoint(0.048, -2.0)],
        {"Current": 4.89, "Market median": 4.85, "Model optimum": 4.84},
    ).to_dict()
    rows = [row for dataset in spec["datasets"].values() for row in dataset]
    curve = [r for r in rows if "objective" in r]
    assert [r["rate"] for r in curve] == [4.7, 4.8]  # fractions plotted as percent
    assert {r["marker"] for r in rows if "marker" in r} == {
        "Current",
        "Market median",
        "Model optimum",
    }


def test_objective_curve_without_markers_is_just_the_curve():
    from balance_pricing_engine.dashboard.charts import objective_curve
    from balance_pricing_engine.dashboard.models import CurvePoint

    spec = objective_curve([CurvePoint(0.047, -3.0)], {}).to_dict()
    rows = [row for dataset in spec["datasets"].values() for row in dataset]
    assert all("marker" not in r for r in rows)
