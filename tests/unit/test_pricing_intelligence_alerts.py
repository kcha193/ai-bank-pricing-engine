"""Alert formatting, kept separate from delivery."""

from datetime import datetime, timedelta, timezone

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.alerts import (
    Alert,
    CollectingAlertSink,
    ConsoleAlertSink,
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


def test_alert_omits_market_context_for_a_different_rate_category(make_record):
    """A "standard" change alongside the CLI's default "special" snapshot must
    not report a median/rank borrowed from the wrong market."""
    standard_change = detect_changes(
        [make_record(rate_category="standard", rate_pct=5.39)],
        [make_record(rate_category="standard", rate_pct=5.19, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )[0]
    special_snapshot = build_market_snapshot(
        [
            make_record(bank="ANZ", rate_category="special", rate_pct=4.79),
            make_record(bank="BNZ", rate_category="special", rate_pct=4.85),
        ]
    )

    rendered = format_alert(
        standard_change, snapshot=special_snapshot, previous_snapshot=special_snapshot
    ).render()

    assert "Market median" not in rendered
    assert "Market rank" not in rendered


def test_alert_omits_market_context_for_a_different_product_with_the_same_category(
    make_record,
):
    """A deposit "special" change alongside a mortgage "special" snapshot must
    not report a median/rank borrowed from the wrong market -- "special"
    exists as a category on both sides, so the rate_category guard alone is
    not enough identity."""
    deposit_change = detect_changes(
        [make_record(product_type=ProductType.TERM_DEPOSIT, rate_pct=4.00)],
        [
            make_record(
                product_type=ProductType.TERM_DEPOSIT, rate_pct=4.20, observed_at=DAY_TWO
            )
        ],
        detected_at=DAY_TWO,
    )[0]
    mortgage_snapshot = build_market_snapshot(
        [
            make_record(bank="ANZ", rate_pct=4.79),
            make_record(bank="BNZ", rate_pct=4.85),
        ]
    )

    rendered = format_alert(
        deposit_change, snapshot=mortgage_snapshot, previous_snapshot=mortgage_snapshot
    ).render()

    assert "TERM DEPOSIT RATE CHANGE" in rendered
    assert "Market median" not in rendered
    assert "Market rank" not in rendered


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


def test_alert_renders_a_non_rate_move_change_type_and_detail(make_record):
    change = detect_changes(
        [make_record(max_lvr=0.80, min_equity=0.20)],
        [make_record(max_lvr=0.75, min_equity=0.25, observed_at=DAY_TWO)],
        detected_at=DAY_TWO,
    )[0]

    rendered = format_alert(change).render()

    assert "Eligibility Change" in rendered
    assert "max_lvr" in rendered


def test_rank_movement_is_omitted_when_the_term_is_missing_from_a_snapshot(make_record):
    change = build_change(make_record)  # ANZ, 12 months
    other_term_snapshot = build_market_snapshot([make_record(bank="ANZ", term_months=24)])

    rendered = format_alert(
        change, snapshot=other_term_snapshot, previous_snapshot=other_term_snapshot
    ).render()

    assert "Market rank:" not in rendered


def test_rank_movement_is_omitted_when_the_bank_is_missing_from_a_snapshot(make_record):
    change = build_change(make_record)  # bank=ANZ
    snapshot_without_anz = build_market_snapshot([make_record(bank="BNZ", rate_pct=4.85)])

    rendered = format_alert(
        change, snapshot=snapshot_without_anz, previous_snapshot=snapshot_without_anz
    ).render()

    assert "Market rank:" not in rendered


def test_rank_movement_reports_unchanged_when_the_rank_is_the_same(make_record):
    snapshot = build_market_snapshot(
        [make_record(bank="ANZ", rate_pct=4.79), make_record(bank="BNZ", rate_pct=4.85)]
    )
    change = build_change(make_record, current_rate=4.79)

    rendered = format_alert(change, snapshot=snapshot, previous_snapshot=snapshot).render()

    assert "(unchanged)" in rendered


def test_market_context_is_omitted_when_the_term_is_missing_from_the_snapshot(make_record):
    change = build_change(make_record)  # ANZ, 12 months
    snapshot_at_other_term = build_market_snapshot([make_record(bank="ANZ", term_months=24)])

    rendered = format_alert(change, snapshot=snapshot_at_other_term).render()

    assert "Market median" not in rendered


def test_console_alert_sink_prints_the_rendered_alert(make_record, capsys):
    ConsoleAlertSink().send(format_alert(build_change(make_record)))

    output = capsys.readouterr().out
    assert "NZ MORTGAGE RATE CHANGE" in output


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
