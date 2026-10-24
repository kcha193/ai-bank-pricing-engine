from dashboard_harness import frame_with, run_page


def test_rate_changes_defaults_to_7_days_and_lists_the_move(market_factory):
    market = market_factory()
    app = run_page("rate_changes", market.database, market.now)
    assert not app.exception
    row = frame_with(app, "Old").iloc[0]
    assert row["Bank"] == "ANZ" and row["Old"] == "4.89%" and row["New"] == "4.79%"
    assert row["Movement"] == "-10 bps" and row["Prev rank"] == 3 and row["New rank"] == 1
    assert row["Term"] == "1Y"


def test_rate_changes_empty_state_is_a_sentence(market_factory):
    market = market_factory(anz_new_rate=4.89)  # no movement
    app = run_page("rate_changes", market.database, market.now)
    assert not app.exception
    assert any("No rate changes" in i.value for i in app.info)


def test_selecting_an_event_shows_provenance_as_data_with_a_safe_link(market_factory):
    market = market_factory()
    app = run_page("rate_changes", market.database, market.now)
    app.selectbox(key="rate_changes_event").select_index(0).run()
    assert not app.exception
    detail = frame_with(app, "Field").set_index("Field")["Value"]
    assert detail["Parser version"] and detail["Rate type"] == "special"
    assert detail["Product"] == "ANZ — Fixed Special"
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Previous rate"] == "4.89%" and metrics["New rate"] == "4.79%"
    assert metrics["Movement"] == "-10 bps" and metrics["Rank"] == "#3 → #1"
    assert [b.proto.url for b in app.get("link_button")] == [
        "https://www.example-bank.co.nz/anz/rates"
    ]
    # scraped text is never rendered through markdown
    assert all("Fixed Special" not in m.value for m in app.markdown)


def test_event_detail_rows_carry_scraped_text_verbatim_as_data():
    from datetime import datetime, timezone

    from balance_pricing_engine.dashboard.models import ChangeRow, EventDetail
    from balance_pricing_engine.dashboard.views.rate_changes import detail_rows

    when = datetime(2026, 10, 10, tzinfo=timezone.utc)
    row = ChangeRow("k", when, "ANZ", 12, 4.89, 4.79, -10.0, "DOWN", "RATE_DECREASE", 3, 1)
    hostile = "![t](http://evil.example/beacon.png) [click](javascript:alert(1)) :red[x]"
    detail = EventDetail(
        row=row,
        product_name=hostile,
        rate_category=hostile,
        source_url="https://e.example/a b)(![x](http://evil.example/p.png)",
        observed_at=when,
        effective_date_text=hostile,
        parser_version=hostile,
        market_median_pct=4.85,
        market_state="NORMAL",
    )
    values = dict(detail_rows(detail))
    # shown exactly as received: a dataframe cell is plain text, so nothing needs escaping
    assert values["Effective date"] == hostile and values["Parser version"] == hostile
    assert values["Rank"] == "#3 → #1" and values["Market median at event"] == "4.85%"


def test_direction_filter_hides_non_matching_rows(market_factory):
    market = market_factory()
    app = run_page("rate_changes", market.database, market.now)
    app.selectbox(key="rate_changes_direction").select("UP").run()
    assert not app.exception
    assert any("No rate changes" in i.value for i in app.info)


def test_a_custom_window_ending_before_it_starts_says_so(market_factory):
    from datetime import date

    market = market_factory()
    app = run_page("rate_changes", market.database, market.now)
    app.radio(key="rate_changes_window").set_value("Custom").run()
    app.date_input(key="rate_changes_from").set_value(date(2026, 10, 20)).run()
    app.date_input(key="rate_changes_to").set_value(date(2026, 10, 10)).run()
    assert not app.exception
    assert any("end date is before the start date" in w.value.lower() for w in app.warning)


def test_out_of_tier_event_remains_visible_with_category_and_unavailable_market_context(
    market_with_tier_event,
):
    from balance_pricing_engine.dashboard.formatting import EM_DASH

    market = market_with_tier_event("non_comparable")
    app = run_page("rate_changes", market.database, market.now)
    assert not app.exception
    rows = frame_with(app, "Old")
    event = rows.loc[rows["New"] == "5.39%"].iloc[0]
    assert event["Rate type"] == "non_comparable"
    assert event["Prev rank"] == event["New rank"] == EM_DASH
    detail = frame_with(app, "Field").set_index("Field")["Value"]
    assert detail["Rate type"] == "non_comparable"
    assert detail["Rank"] == f"{EM_DASH} → {EM_DASH}"
    assert detail["Market median at event"] == EM_DASH


def test_movements_page_summarises_and_charts_the_window(market_factory):
    from dashboard_harness import chart_count

    market = market_factory()
    app = run_page("rate_changes", market.database, market.now)
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["▼ Decrease"] == "1" and metrics["▲ Increase"] == "0"
    assert chart_count(app) == 1
    assert any("**ANZ** · 1Y" in m.value for m in app.markdown)  # the event feed


def test_a_failing_collector_is_reported_as_coverage_not_as_a_withdrawal(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    app = run_page("rate_changes", market.database, market.now)
    assert not app.exception
    notice = " ".join(i.value for i in app.info)
    assert "collection currently failing for Westpac" in notice
    assert "not treated as repricing or withdrawals" in notice
    table = frame_with(app, "Type")
    assert not ((table["Bank"] == "Westpac") & (table["Type"] == "REMOVED_PRODUCT")).any()
