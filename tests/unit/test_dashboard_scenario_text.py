from balance_pricing_engine.dashboard.models import (
    PositionView,
    ScenarioRequest,
    ScenarioRowView,
    ScenarioView,
    SelectedScenario,
)
from balance_pricing_engine.dashboard.scenario_text import build_decision_text


def _view(position):
    rows = (
        ScenarioRowView("Match median", "MARKET_MEDIAN", 0.0485, 0.0484, -8_100_000.0),
        ScenarioRowView("Match leader", "MARKET_LEADER", 0.0479, 0.0478, -9_800_000.0),
        ScenarioRowView("Model optimum", "MODEL_OPTIMUM", 0.0485, 0.0484, -8_100_000.0),
    )
    return ScenarioView(
        ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, 4.89),
        rows,
        SelectedScenario(0.0484, -8_100_000.0, 1e8, 0.0, 0.0485, "x"),
        (),
        position,
        0.0484,
        (),
    )


def test_text_states_gaps_optimum_and_the_disclaimer():
    text = build_decision_text(_view(PositionView(4, 10.0, 4.0, 4.79, 4.85, 5)))
    assert "10 bps above the market leader" in text
    assert "4 bps above the market median" in text
    assert "rank #4 of 5" in text
    assert "model optimum home-loan rate is 4.84%" in text
    assert "-$1.7m" in text  # leader vs median modelled objective, from backend rows
    assert text.rstrip().endswith(
        "Decision support only — this dashboard never changes a bank price."
    )


def test_text_handles_equal_and_below_gaps():
    text = build_decision_text(_view(PositionView(1, 0.0, -2.0, 4.89, 4.91, 5)))
    assert "level with the market leader" in text
    assert "2 bps below the market median" in text


def test_text_without_a_current_rate_omits_position_claims():
    text = build_decision_text(_view(None))
    assert "above the market leader" not in text and "rank #" not in text
    assert "Decision support only" in text


def test_text_makes_no_volume_claims_the_backend_rows_cannot_support():
    text = build_decision_text(_view(PositionView(4, 10.0, 4.0, 4.79, 4.85, 5))).lower()
    assert "volume" not in text
