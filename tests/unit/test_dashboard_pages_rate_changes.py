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


def test_selecting_an_event_shows_provenance_with_a_link(market_factory):
    market = market_factory()
    app = run_page("rate_changes", market.database, market.now)
    app.selectbox(key="rate_changes_event").select_index(0).run()
    assert not app.exception
    text = " ".join(m.value for m in app.markdown)
    assert "Parser version" in text and "/anz/rates" in text
    assert "<html" not in text.lower()  # never raw HTML


def test_event_detail_markdown_treats_scraped_text_as_untrusted():
    from datetime import datetime, timezone

    from balance_pricing_engine.dashboard.models import ChangeRow, EventDetail
    from balance_pricing_engine.dashboard.pages.rate_changes import detail_markdown

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
    text = detail_markdown(detail)
    assert "![" not in text and "[click](" not in text and ":red[" not in text
    assert text.count("](") == 1  # exactly one link: the (sanitised) source URL
    assert "\\!\\[" in text  # escaped (shown as literal text), not silently dropped


def test_direction_filter_hides_non_matching_rows(market_factory):
    market = market_factory()
    app = run_page("rate_changes", market.database, market.now)
    app.selectbox(key="rate_changes_direction").select("UP").run()
    assert not app.exception
    assert any("No rate changes" in i.value for i in app.info)
