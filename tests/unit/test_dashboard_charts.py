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
