import pytest

from dashboard_harness import make_services

from balance_pricing_engine.dashboard.models import ChangeFilters, ChangeWindow


def test_anz_move_has_exact_bps_and_ranks(market_factory):
    market = market_factory()
    rows = make_services(market).changes(ChangeWindow("7d")).value
    anz = next(r for r in rows if r.bank == "ANZ")
    assert (anz.previous_pct, anz.current_pct, anz.change_bps) == (4.89, 4.79, -10.0)
    assert anz.direction == "DOWN" and anz.term_months == 12
    assert anz.previous_rank == 3  # 4.89 tied with ASB/Kiwibank behind the 4.85 pair
    assert anz.new_rank == 1


def test_filters_narrow_the_rows(market_factory):
    market = market_factory()
    services = make_services(market)
    assert services.changes(ChangeWindow("7d"), ChangeFilters(bank="BNZ")).status == "EMPTY"
    assert services.changes(ChangeWindow("7d"), ChangeFilters(direction="UP")).status == "EMPTY"
    assert services.changes(ChangeWindow("7d"), ChangeFilters(term_months=12)).is_ok


def test_today_window_excludes_yesterdays_move(market_factory):
    market = market_factory()
    assert make_services(market).changes(ChangeWindow("today")).status == "EMPTY"


def test_a_window_outside_the_move_is_empty_with_a_sentence(market_factory):
    from datetime import date

    market = market_factory()
    result = make_services(market).changes(
        ChangeWindow("custom", start=date(2026, 9, 1), end=date(2026, 9, 20))
    )
    assert result.status == "EMPTY" and "No rate changes" in result.message


def test_event_detail_exposes_provenance_and_market_context(market_factory):
    market = market_factory()
    services = make_services(market)
    row = services.changes(ChangeWindow("7d")).value[0]
    detail = services.event_detail(row.key).value
    assert detail.source_url.endswith("/anz/rates")
    assert detail.parser_version and detail.rate_category == "special"
    assert detail.market_median_pct == 4.85
    assert detail.market_state in {"LOW", "NORMAL", "ELEVATED", "HIGH"}


def test_unknown_event_key_is_an_empty_result(market_factory):
    result = make_services(market_factory()).event_detail("does-not-exist")
    assert result.status == "EMPTY"


@pytest.mark.parametrize("category", ["special", "standard", "non_comparable", "term_deposit"])
def test_event_ranks_and_detail_context_apply_only_to_the_comparable_tier(
    market_with_tier_event, category
):
    market = market_with_tier_event(category)
    services = make_services(market)
    rows = services.changes(ChangeWindow("7d")).value
    event = next(r for r in rows if r.current_pct == 5.39)
    detail = services.event_detail(event.key).value
    expected = (1, 1) if category == "special" else (None, None)
    assert (event.previous_rank, event.new_rank) == expected
    assert detail.row == event
    assert detail.rate_category == category and detail.product_name == "Audit fixed product"
    assert detail.market_median_pct == (4.85 if category == "special" else None)
    if category != "special":
        assert detail.market_state is None
    # The genuine special move still has its historical before/after ranks.
    comparable = next(r for r in rows if r.current_pct == 4.79)
    assert (comparable.previous_rank, comparable.new_rank) == (3, 1)
    position = next(r for r in services.bank_positions(12).value if r.bank == "ANZ")
    from datetime import timedelta

    assert position.last_change_at == market.now - timedelta(
        hours=12 if category == "special" else 24
    )


@pytest.mark.parametrize("condition", ["lvr", "eligibility"])
def test_event_detail_matches_identity_across_runs(market_factory, make_record, condition):
    from datetime import timedelta
    from balance_pricing_engine.pricing_intelligence.changes import detect_changes
    from balance_pricing_engine.pricing_intelligence.store import ObservationStore

    market = market_factory()
    previous = []
    expected = {}
    with ObservationStore(market.database) as store:
        for i in range(3):
            when = market.now - timedelta(hours=6 - i)
            records = [
                make_record(
                    bank="ASB",
                    product_name="Same name",
                    observed_at=when,
                    rate_pct=4.0 + i * 0.1 + j * 0.01,
                    max_lvr=(0.6 + j * 0.2) if condition == "lvr" else None,
                    min_equity=(0.2 + j * 0.1) if condition == "eligibility" else None,
                    source_url=f"https://example.invalid/source/{i}/{j}",
                    parser_version=f"parser-{i}-{j}",
                    effective_date_text=f"effective-{i}-{j}",
                )
                for j in range(2)
            ]
            run_id = f"siblings-{i}"
            store.record_observations(records, run_id=run_id, observed_at=when)
            events = detect_changes(previous, records, detected_at=when, run_id=run_id)
            store.record_changes(events)
            expected.update({(when, r.advertised_rate_pct): r for r in records})
            previous = records
    services = make_services(market)
    rows = [r for r in services.changes(ChangeWindow("7d")).value if r.bank == "ASB"]
    assert len(rows) >= 4
    for row in rows:
        detail = services.event_detail(row.key).value
        record = expected[(row.detected_at, row.current_pct)]
        # Historical rows intentionally suppress parser evidence refreshed by later runs.
        assert detail.parser_version == "unavailable"
        assert detail.effective_date_text == record.effective_date_text
        assert detail.observed_at == record.captured_at_utc
        assert detail.source_url == record.source_url


@pytest.mark.parametrize("case", ["missing", "sibling", "future"])
def test_event_detail_never_substitutes_unmatched_or_future_metadata(
    market_factory, make_record, case
):
    from datetime import timedelta
    from unittest.mock import patch
    from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService

    market = market_factory()
    services = make_services(market)
    row = next(r for r in services.changes(ChangeWindow("7d")).value if r.bank == "ANZ")
    record = make_record(
        bank="ANZ",
        observed_at=row.detected_at + timedelta(seconds=1),
        parser_version="wrong-parser",
        effective_date_text="wrong-date",
    )
    history = [] if case == "missing" else [record]
    if case == "sibling":
        history = [record.model_copy(update={"max_lvr": 0.8, "captured_at_utc": row.detected_at})]
    with patch.object(PricingIntelligenceService, "get_rate_history", return_value=history):
        detail = services.event_detail(row.key).value
    assert detail.observed_at is None
    assert detail.effective_date_text is None and detail.parser_version is None
    assert detail.source_url.endswith("/anz/rates")  # the event's own source remains usable
    assert detail.row == row
