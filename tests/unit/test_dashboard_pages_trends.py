from dashboard_harness import chart_count, run_page


def test_trends_renders_one_history_chart_for_all_banks_and_a_timeline(market_factory):
    market = market_factory()
    app = run_page("trends", market.database, market.now)
    assert not app.exception
    # history, leader/median, dispersion, timeline, term structure; never one chart per bank
    assert chart_count(app) == 5
    timeline = app.dataframe[0].value
    assert timeline.iloc[0]["Bank"] == "ANZ" and timeline.iloc[0]["Movement"] == "-10 bps"
    assert timeline.iloc[0]["Days after first move"] == 0.0


def test_trends_with_no_history_shows_a_sentence(tmp_path):
    from datetime import datetime, timezone

    app = run_page("trends", tmp_path / "none.db", datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not app.exception
    assert any("No market data" in i.value for i in app.info)


def test_trends_bank_filter_keeps_the_page_working(market_factory):
    market = market_factory()
    app = run_page("trends", market.database, market.now)
    app.multiselect(key="trends_banks").unselect("ASB").run()
    assert not app.exception
