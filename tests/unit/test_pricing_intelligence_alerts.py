"""Alert formatting, kept separate from delivery."""

from datetime import datetime, timedelta, timezone

from balance_pricing_engine.pricing_intelligence.alerts import (
    Alert,
    CollectingAlertSink,
    dispatch_alerts,
    format_alert,
    format_alerts,
    term_name,
)
from balance_pricing_engine.pricing_intelligence.changes import detect_changes
from balance_pricing_engine.pricing_intelligence.config import ValidationThresholds
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot

DAY_ONE = datetime(2026, 8, 20, tzinfo=timezone.utc)
DAY_TWO = DAY_ONE + timedelta(days=2)


def build_change(make_record, previous_rate=4.99, current_rate=4.79, **kwargs):
    return detect_changes(
        [make_record(rate_pct=previous_rate)],
        [make_record(rate_pct=current_rate, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
        **kwargs,
    )[0]


def test_alert_reports_the_movement(make_record):
    alert = format_alert(build_change(make_record))
    rendered = alert.render()

    assert "NZ MORTGAGE RATE CHANGE" in rendered
    assert "ANZ — 1 Year Fixed Special" in rendered
    assert "4.99% → 4.79%" in rendered
    assert "Movement: -20 bps" in rendered
    assert "Detected: 22 Aug 2026" in rendered
    assert "https://" in rendered


def test_alert_adds_market_context_when_a_snapshot_is_supplied(make_record):
    snapshot = build_market_snapshot(
        [
            make_record(bank="ANZ", rate_pct=4.79),
            make_record(bank="BNZ", rate_pct=4.85),
            make_record(bank="ASB", rate_pct=4.89),
        ]
    )
    rendered = format_alert(build_change(make_record), snapshot=snapshot).render()

    assert "Market median: 4.85%" in rendered
    assert "Difference vs median: -6 bps" in rendered


def test_alert_shows_rank_movement_when_both_snapshots_are_supplied(make_record):
    previous = build_market_snapshot(
        [
            make_record(bank="ANZ", rate_pct=4.99),
            make_record(bank="BNZ", rate_pct=4.85),
            make_record(bank="ASB", rate_pct=4.89),
        ]
    )
    current = build_market_snapshot(
        [
            make_record(bank="ANZ", rate_pct=4.79),
            make_record(bank="BNZ", rate_pct=4.85),
            make_record(bank="ASB", rate_pct=4.89),
        ]
    )
    rendered = format_alert(
        build_change(make_record), snapshot=current, previous_snapshot=previous
    ).render()

    assert "Market rank:" in rendered
    assert "#3 → #1" in rendered


def test_alert_omits_rank_movement_without_a_previous_snapshot(make_record):
    """Rank movement cannot be inferred from a rate change alone."""
    assert "Market rank:" not in format_alert(build_change(make_record)).render()


def test_a_suspicious_change_is_marked_for_review(make_record):
    change = build_change(
        make_record,
        previous_rate=4.89,
        current_rate=8.49,
        thresholds=ValidationThresholds(suspicious_change_bps=75),
    )

    assert "FLAGGED SUSPICIOUS" in format_alert(change).render()


def test_withdrawal_and_new_product_alerts_read_correctly(make_record):
    removal = detect_changes([make_record(rate_pct=5.05)], [], detected_at=DAY_TWO)[0]
    addition = detect_changes([], [make_record(rate_pct=4.89)], detected_at=DAY_TWO)[0]

    assert "Withdrawn" in format_alert(removal).render()
    assert "New product advertised at 4.89%" in format_alert(addition).render()


def test_alerts_are_ordered_by_movement_size(make_record):
    changes = detect_changes(
        [make_record(bank="ANZ", rate_pct=4.99), make_record(bank="BNZ", rate_pct=4.95)],
        [
            make_record(bank="ANZ", rate_pct=4.94, observed_at=DAY_TWO),
            make_record(bank="BNZ", rate_pct=4.75, observed_at=DAY_TWO),
        ],
        detected_at=DAY_TWO,
    )
    alerts = format_alerts(changes)

    assert alerts[0].change.bank == "BNZ", "the -20 bps move leads the -5 bps move"


def test_dispatch_sends_every_alert_to_the_sink(make_record):
    sink = CollectingAlertSink()
    sent = dispatch_alerts(format_alerts([build_change(make_record)]), sink)

    assert sent == 1
    assert len(sink.alerts) == 1
    assert isinstance(sink.alerts[0], Alert)


def test_term_names_are_human_readable():
    assert term_name(6) == "6 Month"
    assert term_name(12) == "1 Year"
    assert term_name(18) == "18 Month"
    assert term_name(24) == "2 Year"
    assert term_name(None) == "Floating"
