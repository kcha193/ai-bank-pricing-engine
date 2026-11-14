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


def _rows(chart):
    return [row for rows in chart.to_dict()["datasets"].values() for row in rows]


def _encodings(chart):
    spec = chart.to_dict()
    return [layer.get("encoding", {}) for layer in spec.get("layer", [spec])]


def test_history_lines_is_one_chart_with_a_bank_colour_channel():
    from datetime import date

    from balance_pricing_engine.dashboard.charts import history_lines
    from balance_pricing_engine.dashboard.models import HistoryPoint

    chart = history_lines(
        [HistoryPoint(date(2026, 10, 1), "ANZ", 4.9), HistoryPoint(date(2026, 10, 1), "BNZ", 4.85)]
    )
    assert all(e["color"]["field"] == "bank" for e in _encodings(chart))
    assert len(_rows(chart)) == 2


def test_history_lines_break_instead_of_bridging_a_missing_period():
    """A bank absent on a covered date gets a gap row, never an interpolated rate."""
    from datetime import date

    from balance_pricing_engine.dashboard.charts import history_lines
    from balance_pricing_engine.dashboard.models import HistoryPoint

    days = [date(2026, 10, d) for d in (1, 2, 3)]
    chart = history_lines(
        [
            HistoryPoint(days[0], "ANZ", 4.9),
            HistoryPoint(days[2], "ANZ", 4.8),
            HistoryPoint(days[0], "BNZ", 4.85),
            HistoryPoint(days[1], "BNZ", 4.85),
            HistoryPoint(days[2], "BNZ", 4.85),
        ],
        days=days,
        selected_bank="ANZ",
    )
    anz = sorted((r["day"], r["rate"]) for r in _rows(chart) if r["bank"] == "ANZ")
    import pandas as pd

    assert [None if pd.isna(rate) else rate for _, rate in anz] == [4.9, None, 4.8]
    assert {r["selected"] for r in _rows(chart) if r["bank"] == "ANZ"} == {True}
    assert chart.to_dict()["layer"][0]["mark"]["invalid"] == "break-paths-show-domains"


def test_market_levels_and_dispersion_keep_percent_and_bps_on_separate_charts():
    from datetime import date

    from balance_pricing_engine.dashboard.charts import market_dispersion, market_levels
    from balance_pricing_engine.dashboard.models import MarketPoint

    market = [MarketPoint(date(2026, 10, 1), 4.85, 4.79, 10.0, ("ANZ",))]
    levels = {r["series"]: r["value"] for r in _rows(market_levels(market))}
    assert levels == {"Market median": 4.85, "Market leader": 4.79}
    assert [r["spread"] for r in _rows(market_dispersion(market))] == [10.0]
    spec = market_dispersion(market).to_dict()
    assert spec["encoding"]["y"]["title"] == "Dispersion (bps)"
    assert spec["encoding"]["y"]["scale"]["zero"] is True


def test_gap_bars_plot_the_backend_bps_for_leader_and_median():
    from balance_pricing_engine.dashboard.charts import gap_bars

    positions = [
        BankPosition("ANZ", 4.79, 1, 0.0, -6.0, None, FreshnessState.FRESH),
        BankPosition("ASB", 4.89, 4, 10.0, 4.0, None, FreshnessState.FRESH),
    ]
    rows = {(r["bank"], r["measure"]): r["bps"] for r in _rows(gap_bars(positions, "ASB")) if "bank" in r}
    assert rows == {
        ("ANZ", "vs leader"): 0.0,
        ("ANZ", "vs median"): -6.0,
        ("ASB", "vs leader"): 10.0,
        ("ASB", "vs median"): 4.0,
    }


def test_term_structure_leaves_out_stale_cells_and_flags_quarantined_fallbacks():
    from balance_pricing_engine.dashboard.charts import term_structure
    from balance_pricing_engine.dashboard.models import MarketMatrix, MatrixCell

    matrix = MarketMatrix(
        ("ANZ", "ASB"),
        (6, 12),
        {
            ("ANZ", 12): MatrixCell(4.79, is_leader=True),
            ("ASB", 12): MatrixCell(4.89, quarantined=True),
            ("ASB", 6): MatrixCell(4.50, stale=True),
        },
    )
    rows = {(r["bank"], r["term"]): r for r in _rows(term_structure(matrix))}
    assert rows[("ASB", "6M")]["rate"] is None  # stale: never plotted
    assert rows[("ASB", "1Y")]["status"].startswith("Quarantined")
    assert rows[("ANZ", "1Y")]["status"] == "Term leader"


def test_movement_timeline_groups_events_by_type_and_keeps_non_rate_events():
    from datetime import datetime, timezone

    from balance_pricing_engine.dashboard.charts import movement_timeline
    from balance_pricing_engine.dashboard.models import ChangeRow

    when = datetime(2026, 10, 10, tzinfo=timezone.utc)
    rows = _rows(
        movement_timeline(
            [
                ChangeRow("a", when, "ANZ", 12, 4.89, 4.79, -10.0, "DOWN", "RATE_DECREASE", 3, 1),
                ChangeRow("b", when, "BNZ", 12, None, 4.99, None, "FLAT", "NEW_PRODUCT", None, None),
                ChangeRow("c", when, "ASB", 12, 4.89, None, None, "FLAT", "REMOVED_PRODUCT", 2, None),
            ]
        )
    )
    assert {r["bank"]: r["direction"] for r in rows} == {
        "ANZ": "Decrease",
        "BNZ": "New product",
        "ASB": "Withdrawn",
    }


def test_freshness_bars_mark_the_backend_limit(market_factory):
    from dashboard_harness import make_services

    from balance_pricing_engine.dashboard.charts import freshness_bars

    health = make_services(market_factory(stale_banks=("Westpac",))).health().value
    rows = _rows(freshness_bars(health))
    states = {r["bank"]: r["state"] for r in rows if "bank" in r}
    assert states["Westpac"] == "Stale" and states["ANZ"] == "Fresh"
    assert [r["age"] for r in rows if "text" in r] == [health.max_observation_age_hours]


def test_repricing_timeline_plots_one_dot_per_event():
    from datetime import datetime, timezone

    from balance_pricing_engine.dashboard.charts import repricing_timeline
    from balance_pricing_engine.dashboard.models import TimelineEvent

    when = datetime(2026, 10, 10, tzinfo=timezone.utc)
    chart = repricing_timeline(
        [TimelineEvent(when, "ANZ", -10.0, 0.0), TimelineEvent(when, "BNZ", 15.0, 2.0)]
    )
    assert {(r["bank"], r["direction"]) for r in _rows(chart)} == {
        ("ANZ", "Decrease"),
        ("BNZ", "Increase"),
    }


def test_objective_curve_plots_the_curve_in_percent_and_annotates_each_marker_once():
    from balance_pricing_engine.dashboard.charts import objective_curve
    from balance_pricing_engine.dashboard.models import CurvePoint

    spec = objective_curve(
        [CurvePoint(0.047, -3.0), CurvePoint(0.048, -2.0)],
        {"Current": 4.89, "Market median": 4.85, "Median-benchmark optimum (reference)": 4.84},
    ).to_dict()
    rows = [row for dataset in spec["datasets"].values() for row in dataset]
    curve = [r for r in rows if "objective" in r]
    assert [r["rate"] for r in curve] == [4.7, 4.8]  # fractions plotted as percent
    assert {r["marker"] for r in rows if "marker" in r} == {
        "Current",
        "Market median",
        "Median-benchmark optimum (reference)",
    }


def test_objective_curve_without_markers_is_just_the_curve():
    from balance_pricing_engine.dashboard.charts import objective_curve
    from balance_pricing_engine.dashboard.models import CurvePoint

    spec = objective_curve([CurvePoint(0.047, -3.0)], {}).to_dict()
    rows = [row for dataset in spec["datasets"].values() for row in dataset]
    assert all("marker" not in r for r in rows)
