"""Daily/weekly brief rendering, driven by directly-constructed fixtures."""

from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.briefing import build_daily_brief, build_weekly_brief
from balance_pricing_engine.pricing_intelligence.changes import detect_changes
from balance_pricing_engine.pricing_intelligence.market_state import MarketState, MarketStateAssessment
from balance_pricing_engine.pricing_intelligence.positioning import MarketActivity
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot
from balance_pricing_engine.pricing_intelligence.store import CollectorHealth, RunRecord

DAY_ONE = datetime(2026, 8, 20, tzinfo=timezone.utc)
DAY_TWO = DAY_ONE + timedelta(days=1)
MARKET = [("ANZ", 4.79), ("BNZ", 4.85), ("Westpac", 4.85), ("ASB", 4.89), ("Kiwibank", 4.89)]


def _assessment(**overrides):
    activity = MarketActivity(
        lookback_days=7,
        changes=1,
        banks_changed=1,
        terms_changed=1,
        increases=0,
        decreases=1,
        mean_change_bps=-20.0,
        dominant_direction="DOWN",
    )
    payload = dict(state=MarketState.ELEVATED, activity=activity, rationale="1 change, 1 bank")
    payload.update(overrides)
    return MarketStateAssessment(**payload)


def _health(bank, consecutive_failures=0, last_success_at=DAY_TWO):
    return CollectorHealth(
        bank=bank,
        product_type=ProductType.HOME_LOAN,
        consecutive_failures=consecutive_failures,
        last_success_at=last_success_at,
        last_failure_at=None,
        last_error=None,
        last_alert_sent_at=None,
    )


def _run_record(run_id, banks_attempted, banks_successful):
    return RunRecord(
        run_id=run_id,
        started_at=DAY_ONE,
        completed_at=DAY_ONE,
        status="SUCCESS" if set(banks_attempted) == set(banks_successful) else "PARTIAL_SUCCESS",
        product_type=ProductType.HOME_LOAN,
        banks_attempted=tuple(banks_attempted),
        banks_successful=tuple(banks_successful),
        banks_failed=tuple(sorted(set(banks_attempted) - set(banks_successful))),
        rates_observed=10,
        rates_changed=0,
        validation_failures=0,
        suspicious_observations=0,
        market_state="LOW",
        error_summary=None,
    )


@pytest.fixture
def snapshot(make_record):
    records = [make_record(bank=bank, rate_pct=rate) for bank, rate in MARKET]
    return build_market_snapshot(records)


def test_no_change_day_is_a_single_line(snapshot):
    brief = build_daily_brief(
        as_of=DAY_TWO,
        today_changes=[],
        week_assessment=_assessment(),
        snapshot=snapshot,
        collector_health=[_health(bank) for bank, _ in MARKET],
        tracked_banks=[bank for bank, _ in MARKET],
    )
    assert brief == (
        "NZ PRICING INTELLIGENCE\n21 Aug 2026\n\n"
        "No material mortgage pricing changes detected today."
    )


def test_a_reprice_day_reports_the_move_and_market_context(make_record, snapshot):
    change = detect_changes(
        [make_record(bank="ANZ", rate_pct=4.79)],
        [make_record(bank="ANZ", rate_pct=4.59, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )[0]

    brief = build_daily_brief(
        as_of=DAY_TWO,
        today_changes=[change],
        week_assessment=_assessment(),
        snapshot=snapshot,
        collector_health=[_health(bank) for bank, _ in MARKET],
        tracked_banks=[bank for bank, _ in MARKET],
        scenario_triggered_terms=[12],
    )

    assert "TODAY" in brief
    assert "ANZ 1 Year    -20 bps" in brief
    assert "Market state: ELEVATED" in brief
    assert "MOST COMPETITIVE" in brief
    assert "1 Year: ANZ" in brief
    assert "WIDEST SPREAD" in brief
    assert "COLLECTOR HEALTH" in brief
    assert "5/5 banks fresh" in brief
    assert "Pricing scenario analysis triggered for:" in brief
    assert "1 Year" in brief.splitlines()[-1]


def test_a_bank_with_a_failure_streak_is_not_counted_fresh(make_record, snapshot):
    health = [_health(bank) for bank, _ in MARKET]
    health[0] = _health("ANZ", consecutive_failures=2)

    brief = build_daily_brief(
        as_of=DAY_TWO,
        today_changes=detect_changes(
            [make_record(bank="ANZ", rate_pct=4.79)],
            [make_record(bank="ANZ", rate_pct=4.59, observed_at=DAY_TWO)],
            detected_at=DAY_TWO,
        ),
        week_assessment=_assessment(),
        snapshot=snapshot,
        collector_health=health,
        tracked_banks=[bank for bank, _ in MARKET],
    )

    assert "4/5 banks fresh" in brief


def test_a_removed_bank_is_not_counted_in_the_freshness_ratio(make_record, snapshot):
    """A bank dropped from selected_banks after health history accumulated
    still has a collector_health row; it must not be counted in the
    numerator against a denominator of only currently-tracked banks, or the
    brief can report an impossible ratio like 6/5 banks fresh."""
    health = [_health(bank) for bank, _ in MARKET]
    health.append(_health("Rabobank"))  # no longer in tracked_banks

    brief = build_daily_brief(
        as_of=DAY_TWO,
        today_changes=detect_changes(
            [make_record(bank="ANZ", rate_pct=4.79)],
            [make_record(bank="ANZ", rate_pct=4.59, observed_at=DAY_TWO)],
            detected_at=DAY_TWO,
        ),
        week_assessment=_assessment(),
        snapshot=snapshot,
        collector_health=health,
        tracked_banks=[bank for bank, _ in MARKET],
    )

    assert "5/5 banks fresh" in brief
    assert "6/5" not in brief


def test_an_aged_bank_with_no_failure_streak_is_not_counted_fresh(make_record, snapshot):
    """A bank that has simply stopped being attempted (scheduling disabled,
    the machine off for a week) never accumulates consecutive_failures, so
    the failures-only check would count it fresh forever -- contradicting
    the health command and dashboard, which already apply
    max_observation_age_hours to flag exactly this as STALE. Passing the
    same threshold here must exclude it from the numerator too."""
    as_of = DAY_TWO + timedelta(hours=200)
    health = [_health(bank, last_success_at=as_of - timedelta(hours=10)) for bank, _ in MARKET]
    health[0] = _health("ANZ", last_success_at=as_of - timedelta(hours=200))

    brief = build_daily_brief(
        as_of=as_of,
        today_changes=detect_changes(
            [make_record(bank="BNZ", rate_pct=4.85)],
            [make_record(bank="BNZ", rate_pct=4.65, observed_at=as_of)],
            detected_at=as_of,
        ),
        week_assessment=_assessment(),
        snapshot=snapshot,
        collector_health=health,
        tracked_banks=[bank for bank, _ in MARKET],
        max_observation_age_hours=168,
    )

    assert "4/5 banks fresh" in brief


def test_weekly_brief_with_no_changes(snapshot):
    brief = build_weekly_brief(
        as_of=DAY_TWO,
        week_changes=[],
        week_assessment=_assessment(),
        snapshot=snapshot,
        run_records=[],
    )
    assert "No repricing events this week." in brief
    assert "NZ MORTGAGE PRICING -- WEEKLY" in brief


def test_weekly_brief_reports_largest_move_and_most_active_term(make_record, snapshot):
    changes = detect_changes(
        [make_record(bank="ANZ", rate_pct=4.79), make_record(bank="BNZ", rate_pct=4.85)],
        [
            make_record(bank="ANZ", rate_pct=4.59, observed_at=DAY_TWO),
            make_record(bank="BNZ", rate_pct=4.80, observed_at=DAY_TWO),
        ],
        detected_at=DAY_TWO,
    )

    brief = build_weekly_brief(
        as_of=DAY_TWO,
        week_changes=changes,
        week_assessment=_assessment(),
        snapshot=snapshot,
        run_records=[],
    )

    assert "Repricing events: 2" in brief
    assert "Banks changing: 2" in brief
    assert "Largest move:" in brief
    assert "ANZ 1 Year -20 bps" in brief
    assert "Most active term: 1 Year" in brief


def test_a_suspicious_move_is_flagged_not_hidden_in_daily_brief(make_record, snapshot):
    """A suspicious move must still be listed under TODAY (it is a genuine
    detected event) but flagged, matching the "FLAGGED SUSPICIOUS" warning a
    live alert already carries for the same change -- never presented as
    ordinary, trusted repricing."""
    suspicious_change = detect_changes(
        [make_record(bank="ANZ", rate_pct=4.79)],
        # Far larger than suspicious_change_bps (75 bps default).
        [make_record(bank="ANZ", rate_pct=8.49, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )[0]
    assert suspicious_change.suspicious

    brief = build_daily_brief(
        as_of=DAY_TWO,
        today_changes=[suspicious_change],
        week_assessment=_assessment(),
        snapshot=snapshot,
        collector_health=[_health(bank) for bank, _ in MARKET],
        tracked_banks=[bank for bank, _ in MARKET],
    )

    assert "TODAY" in brief
    assert "ANZ 1 Year" in brief
    assert "[SUSPICIOUS -- verify source]" in brief


def test_weekly_brief_excludes_suspicious_moves_from_aggregate_stats(make_record, snapshot):
    """Suspicious movements must be excluded from every weekly aggregate --
    repricing-event count, banks changing, largest move, most active term --
    the same as compute_market_activity already excludes them from
    market-state classification. A parser fault must not be able to fake a
    repricing round here either."""
    normal_change = detect_changes(
        [make_record(bank="BNZ", rate_pct=4.85)],
        [make_record(bank="BNZ", rate_pct=4.80, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )[0]
    suspicious_change = detect_changes(
        [make_record(bank="ANZ", rate_pct=4.79)],
        [make_record(bank="ANZ", rate_pct=8.49, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )[0]
    assert suspicious_change.suspicious
    assert not normal_change.suspicious

    brief = build_weekly_brief(
        as_of=DAY_TWO,
        week_changes=[normal_change, suspicious_change],
        week_assessment=_assessment(),
        snapshot=snapshot,
        run_records=[],
    )

    assert "Repricing events: 1" in brief
    assert "Banks changing: 1" in brief
    assert "BNZ 1 Year -5 bps" in brief, "the suspicious ANZ move must not win 'Largest move'"
    assert "ANZ" not in brief.split("Largest move:")[1].split("\n\n")[0]
    assert "1 suspicious movement(s) excluded" in brief


def test_daily_brief_labels_a_term_deposit_snapshot(make_record):
    deposit_snapshot = build_market_snapshot(
        [make_record(bank=bank, rate_pct=rate, product_type="term_deposit", rate_category="term_deposit")
            for bank, rate in MARKET],
        product_type=ProductType.TERM_DEPOSIT,
        rate_category="term_deposit",
    )

    brief = build_daily_brief(
        as_of=DAY_TWO,
        today_changes=[],
        week_assessment=_assessment(),
        snapshot=deposit_snapshot,
        collector_health=[_health(bank) for bank, _ in MARKET],
        tracked_banks=[bank for bank, _ in MARKET],
    )

    assert "No material term deposit pricing changes detected today." in brief
    assert "mortgage" not in brief.lower()


def test_weekly_brief_labels_a_term_deposit_snapshot(make_record):
    deposit_snapshot = build_market_snapshot(
        [make_record(bank=bank, rate_pct=rate, product_type="term_deposit", rate_category="term_deposit")
            for bank, rate in MARKET],
        product_type=ProductType.TERM_DEPOSIT,
        rate_category="term_deposit",
    )

    brief = build_weekly_brief(
        as_of=DAY_TWO,
        week_changes=[],
        week_assessment=_assessment(),
        snapshot=deposit_snapshot,
        run_records=[],
    )

    assert "NZ TERM DEPOSIT PRICING -- WEEKLY" in brief
    assert "mortgage" not in brief.lower()


def test_weekly_brief_reports_collector_uptime(snapshot):
    run_records = [
        _run_record("r1", ["ANZ", "BNZ"], ["ANZ", "BNZ"]),
        _run_record("r2", ["ANZ", "BNZ"], ["ANZ"]),
    ]

    brief = build_weekly_brief(
        as_of=DAY_TWO,
        week_changes=[],
        week_assessment=_assessment(),
        snapshot=snapshot,
        run_records=run_records,
    )

    assert "Collector uptime:" in brief
    assert "ANZ           100%" in brief
    assert "BNZ            50%" in brief
