"""Dashboard data shaping, tested without Streamlit."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.changes import detect_changes
from balance_pricing_engine.pricing_intelligence.dashboard_data import (
    build_changes_frame,
    build_collector_health_frame,
    build_freshness_frame,
    build_history_frame,
    build_history_series,
    build_market_grid_frame,
    build_ranking_frame,
    build_recent_runs_frame,
    build_scenario_runs_frame,
    build_term_summary,
)
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot
from balance_pricing_engine.pricing_intelligence.store import CollectorHealth, RunRecord

DAY_ONE = datetime(2026, 8, 20, tzinfo=timezone.utc)
DAY_TWO = DAY_ONE + timedelta(days=1)
MARKET = [("ANZ", 4.79), ("BNZ", 4.85), ("Westpac", 4.85), ("ASB", 4.89), ("Kiwibank", 4.89)]


@pytest.fixture
def snapshot(make_record):
    records = [make_record(bank=bank, rate_pct=rate) for bank, rate in MARKET]
    records.append(make_record(bank="ANZ", term_months=24, rate_pct=4.95))
    return build_market_snapshot(records)


def test_market_grid_has_a_row_per_bank_and_summary_rows(snapshot):
    frame = build_market_grid_frame(snapshot)

    assert "1Y" in frame.columns
    assert "2Y" in frame.columns
    assert frame.loc["ANZ", "1Y"] == 4.79
    assert frame.loc["Median", "1Y"] == 4.85
    assert frame.loc["Spread (bps)", "1Y"] == 10.0


def test_market_grid_leaves_a_gap_where_a_bank_has_no_rate(snapshot):
    assert frame_is_missing(build_market_grid_frame(snapshot), "BNZ", "2Y")


def frame_is_missing(frame, row, column):
    return frame.loc[row, column] is None or frame.loc[row, column] != frame.loc[row, column]


def test_market_grid_of_an_empty_snapshot_is_empty(make_record):
    assert build_market_grid_frame(build_market_snapshot([])).empty


def test_term_summary_reports_headline_statistics(snapshot):
    summary = build_term_summary(snapshot, 12)

    assert summary["best_rate"] == 4.79
    assert summary["median_rate"] == 4.85
    assert summary["highest_rate"] == 4.89
    assert summary["spread_bps"] == 10.0
    assert summary["market_leader"] == "ANZ"
    assert summary["bank_count"] == 5


def test_term_summary_of_an_unknown_term_is_empty(snapshot):
    assert build_term_summary(snapshot, 120) == {}


def test_ranking_frame_preserves_ties(snapshot):
    frame = build_ranking_frame(snapshot, 12)

    assert list(frame["Rank"]) == [1, 2, 2, 4, 4]
    assert frame.iloc[0]["Bank"] == "ANZ"


def test_changes_frame_lists_recent_repricing(make_record):
    changes = detect_changes(
        [make_record(bank="ANZ", rate_pct=4.99)],
        [make_record(bank="ANZ", rate_pct=4.79, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )
    frame = build_changes_frame(changes)

    assert frame.iloc[0]["Bank"] == "ANZ"
    assert frame.iloc[0]["Move (bps)"] == -20.0
    assert frame.iloc[0]["From (%)"] == 4.99


def test_changes_frame_of_a_quiet_market_is_empty():
    assert build_changes_frame([]).empty


def test_history_frame_is_sorted_oldest_first(make_record):
    records = [
        make_record(rate_pct=4.79, observed_at=DAY_TWO),
        make_record(rate_pct=4.99, observed_at=DAY_ONE),
    ]
    frame = build_history_frame(records)

    assert list(frame["Rate (%)"]) == [4.99, 4.79]


def test_history_series_pivots_to_date_by_bank():
    series = [
        (DAY_ONE.date(), {"ANZ": 4.99, "BNZ": 4.95}),
        (DAY_TWO.date(), {"ANZ": 4.79, "BNZ": 4.95}),
    ]

    frame = build_history_series(series)

    assert list(frame.columns) == ["ANZ", "BNZ"]
    assert frame.loc[DAY_TWO.date(), "ANZ"] == 4.79


def test_history_series_gaps_a_bank_absent_from_a_dates_representative_rates():
    """get_rate_history_series() already resolves withdrawals and multi-product
    selection against the store; a bank missing from a date's dict (withdrawn,
    or not yet observed) must chart as a genuine gap, not be filled forward."""
    series = [
        (DAY_ONE.date(), {"ANZ": 4.79, "BNZ": 4.85}),
        (DAY_TWO.date(), {"BNZ": 4.75}),
    ]

    frame = build_history_series(series)

    assert frame.loc[DAY_ONE.date(), "ANZ"] == 4.79
    assert frame.loc[DAY_TWO.date(), "ANZ"] != frame.loc[DAY_TWO.date(), "ANZ"], "expected NaN"


def test_history_series_sorts_dates_regardless_of_input_order():
    series = [
        (DAY_TWO.date(), {"ANZ": 4.79}),
        (DAY_ONE.date(), {"ANZ": 4.99}),
    ]

    frame = build_history_series(series)

    assert list(frame.index) == [DAY_ONE.date(), DAY_TWO.date()]


def test_history_series_of_nothing_is_empty():
    assert build_history_series([]).empty


def test_freshness_frame_of_an_empty_bank_set_is_empty(make_record):
    """PricingIntelligenceConfig accepts selected_banks=[] as valid, and the
    dashboard only catches OSError/sqlite3.Error -- an unguarded
    pd.DataFrame([]).set_index("Bank") would raise KeyError (no "Bank"
    column on an empty frame) and abort the whole page instead."""
    frame = build_freshness_frame(
        [make_record(bank="ANZ")],
        [],
        max_age_hours=168,
        now=DAY_ONE,
        run_ids_by_bank={"ANZ": ["run-1"]},
        latest_run_id="run-1",
    )

    assert frame.empty
    assert list(frame.columns) == ["Oldest confirmation (UTC)", "Age (hours)", "Status"]
    assert frame.index.name == "Bank"


def test_freshness_frame_flags_a_bank_not_touched_by_the_latest_run(make_record):
    frame = build_freshness_frame(
        [make_record(bank="ANZ", observed_at=DAY_ONE)],
        ["ANZ"],
        max_age_hours=168,
        now=DAY_ONE,
        run_ids_by_bank={"ANZ": ["run-1"]},
        latest_run_id="run-2",
    )

    assert "not fully confirmed in latest stored run" in frame.loc["ANZ", "Status"]


def test_freshness_frame_of_a_bank_touched_by_the_latest_run_is_fresh(make_record):
    frame = build_freshness_frame(
        [make_record(bank="ANZ", observed_at=DAY_ONE)],
        ["ANZ"],
        max_age_hours=168,
        now=DAY_ONE,
        run_ids_by_bank={"ANZ": ["run-1"]},
        latest_run_id="run-1",
    )

    assert frame.loc["ANZ", "Status"] == "Fresh"


def _health(bank, consecutive_failures=0, last_error=None):
    return CollectorHealth(
        bank=bank,
        product_type=ProductType.HOME_LOAN,
        consecutive_failures=consecutive_failures,
        last_success_at=DAY_ONE,
        last_failure_at=None,
        last_error=last_error,
        last_alert_sent_at=None,
    )


def test_collector_health_frame_of_no_data_is_empty_with_the_right_columns():
    frame = build_collector_health_frame([], [])

    assert frame.empty
    assert list(frame.columns) == ["Consecutive failures", "Status", "Last success", "Last error"]


def test_collector_health_frame_marks_failing_banks():
    frame = build_collector_health_frame(
        [_health("ANZ"), _health("Westpac", 3, "ParserError")], ["ANZ", "Westpac"]
    )

    assert frame.loc["ANZ", "Status"] == "FRESH"
    assert frame.loc["Westpac", "Status"] == "FAILING"
    assert frame.loc["Westpac", "Last error"] == "ParserError"


def test_collector_health_frame_marks_a_stale_success_once_past_the_freshness_window():
    """A bank that stops being attempted at all (scheduling disabled) never
    fails again, so consecutive_failures alone would report it FRESH
    indefinitely -- even once its last success is older than
    max_observation_age_hours, directly contradicting the adjacent
    freshness table/market snapshot, which do apply that same threshold --
    matching the pricing_intelligence health CLI command's own check."""
    frame = build_collector_health_frame(
        [_health("ANZ")],
        ["ANZ"],
        now=DAY_ONE + timedelta(hours=2),
        max_observation_age_hours=1.0,
    )

    assert frame.loc["ANZ", "Status"] == "STALE"


def test_collector_health_frame_without_freshness_params_stays_fresh_indefinitely():
    """Backward-compatible default: omitting now/max_observation_age_hours
    never classifies a zero-failure bank as anything but FRESH."""
    frame = build_collector_health_frame([_health("ANZ")], ["ANZ"])

    assert frame.loc["ANZ", "Status"] == "FRESH"


def test_collector_health_frame_shows_a_newly_tracked_bank_as_unknown():
    """A bank just added to selected_banks has no collector_health row yet --
    it must appear as UNKNOWN, not be silently omitted from the table."""
    frame = build_collector_health_frame([_health("ANZ")], ["ANZ", "BNZ"])

    assert list(frame.index) == ["ANZ", "BNZ"]
    assert frame.loc["BNZ", "Status"] == "UNKNOWN"
    assert frame.loc["BNZ", "Consecutive failures"] == 0
    assert pd.isna(frame.loc["BNZ", "Last success"])


def test_collector_health_frame_drops_a_bank_no_longer_tracked():
    """A bank removed from selected_banks must not linger in the table just
    because its old collector_health row is still persisted."""
    frame = build_collector_health_frame([_health("ANZ"), _health("Westpac")], ["ANZ"])

    assert list(frame.index) == ["ANZ"]
    assert "Westpac" not in frame.index


def _run_record(run_id, status="SUCCESS"):
    return RunRecord(
        run_id=run_id,
        started_at=DAY_ONE,
        completed_at=DAY_ONE,
        status=status,
        product_type=ProductType.HOME_LOAN,
        banks_attempted=("ANZ", "BNZ"),
        banks_successful=("ANZ", "BNZ") if status == "SUCCESS" else ("ANZ",),
        banks_failed=() if status == "SUCCESS" else ("BNZ",),
        rates_observed=10,
        rates_changed=1,
        validation_failures=0,
        suspicious_observations=0,
        market_state="LOW",
        error_summary=None,
    )


def test_recent_runs_frame_of_no_runs_is_empty():
    assert build_recent_runs_frame([]).empty


def test_recent_runs_frame_reports_bank_counts_per_run():
    frame = build_recent_runs_frame([_run_record("r1"), _run_record("r2", status="PARTIAL_SUCCESS")])

    assert list(frame["Run id"]) == ["r1", "r2"]
    assert list(frame["Banks OK"]) == [2, 1]
    assert list(frame["Banks failed"]) == [0, 1]


def test_scenario_runs_frame_of_no_runs_is_empty():
    assert build_scenario_runs_frame([]).empty


def test_scenario_runs_frame_flattens_scenarios_and_converts_rates_to_percent():
    scenario_runs = [
        {
            "term_months": 12,
            "generated_at": DAY_ONE,
            "scenarios": [
                {"label": "No change", "competitor_rate": 0.0489, "our_rate": 0.0549, "objective": 100.0},
                {"label": "Match median", "competitor_rate": 0.0485, "our_rate": 0.0545, "objective": 90.0},
            ],
        }
    ]

    frame = build_scenario_runs_frame(scenario_runs)

    assert len(frame) == 2
    assert frame.iloc[0]["Term"] == "1Y"
    assert frame.iloc[0]["Scenario"] == "No change"
    assert frame.iloc[0]["Competitor rate (%)"] == pytest.approx(4.89)
    assert frame.iloc[0]["Our rate (%)"] == pytest.approx(5.49)
