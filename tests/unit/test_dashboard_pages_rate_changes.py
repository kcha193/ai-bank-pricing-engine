from dashboard_harness import run_page


def test_rate_changes_defaults_to_7_days_and_lists_the_move(market_factory):
    market = market_factory()
    app = run_page("rate_changes", market.database, market.now)
    assert not app.exception
    row = app.dataframe[0].value.iloc[0]
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
    detail = app.dataframe[1].value.set_index("Field")["Value"]
    assert detail["Parser version"] and detail["Rate type"] == "special"
    assert detail["Product"] == "ANZ — Fixed Special"
    assert [b.proto.url for b in app.get("link_button")] == [
        "https://www.example-bank.co.nz/anz/rates"
    ]
    # scraped text is never rendered through markdown
    assert all("Fixed Special" not in m.value for m in app.markdown)


def test_event_detail_rows_carry_scraped_text_verbatim_as_data():
    from datetime import datetime, timezone

    from balance_pricing_engine.dashboard.models import ChangeRow, EventDetail
    from balance_pricing_engine.dashboard.pages.rate_changes import detail_rows

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
