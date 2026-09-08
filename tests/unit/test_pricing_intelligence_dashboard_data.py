"""Dashboard data shaping, tested without Streamlit."""

from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.pricing_intelligence.changes import detect_changes
from balance_pricing_engine.pricing_intelligence.dashboard_data import (
    build_changes_frame,
    build_freshness_frame,
    build_history_frame,
    build_history_series,
    build_market_grid_frame,
    build_ranking_frame,
    build_term_summary,
)
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot

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
