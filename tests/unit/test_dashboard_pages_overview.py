from datetime import datetime, timezone

from dashboard_harness import run_page


def test_overview_shows_matrix_summary_and_ranking_with_ties(market_factory):
    market = market_factory()
    app = run_page("overview", market.database, market.now)
    assert not app.exception
    matrix = app.dataframe[0].value
    assert matrix.loc["ANZ", "1Y"] == "◆ 4.79%"
    assert matrix.loc["ASB", "1Y"] == "4.89%"
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Lowest"] == "4.79%" and metrics["Median"] == "4.85%"
    assert metrics["Highest"] == "4.89%" and metrics["Spread"] == "10 bps"
    assert metrics["Market leader"] == "ANZ"
    ranking = app.dataframe[1].value
    assert list(ranking["Rank"]) == [1, 2, 2, 4, 4]


def test_overview_marks_stale_bank_and_explains_exclusion(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    app = run_page("overview", market.database, market.now)
    assert app.dataframe[0].value.loc["Westpac", "1Y"] == "⚠ 4.85% (stale)"
    assert any("Westpac excluded" in w.value and "168" in w.value for w in app.warning)


def test_overview_without_data_shows_guidance_not_raw_values(tmp_path):
    app = run_page("overview", tmp_path / "none.db", datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not app.exception
    assert any("No market data" in i.value for i in app.info)


def test_overview_with_a_corrupt_database_shows_an_operational_error(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("not a SQLite database")
    app = run_page("overview", bad, datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not app.exception
    assert any("unavailable" in e.value.lower() for e in app.error)


def test_term_selector_offers_the_tracked_term_labels(market_factory):
    market = market_factory()
    app = run_page("overview", market.database, market.now)
    assert app.selectbox[0].options == ["1Y"]
