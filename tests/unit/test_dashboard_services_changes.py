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
