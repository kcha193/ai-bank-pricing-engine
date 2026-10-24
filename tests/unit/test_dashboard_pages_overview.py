from datetime import datetime, timezone

from dashboard_harness import chart_count, run_page


def test_command_centre_shows_pulse_story_and_matrix(market_factory):
    market = market_factory()
    app = run_page("overview", market.database, market.now)
    assert not app.exception
    matrix = app.dataframe[0].value
    assert matrix.loc["ANZ", "1Y"] == "◆ 4.79%"
    assert matrix.loc["ASB", "1Y"] == "4.89%"
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Lowest rate"] == "4.79%" and metrics["Market median"] == "4.85%"
    assert metrics["Competitive spread"] == "10 bps"
    assert metrics["Mortgage leader"] == "ANZ" and metrics["Fresh coverage"] == "5 / 5"
    assert metrics["Repricing · 7d"] == "1"
    assert chart_count(app) == 1  # the market-story ranking chart


def test_command_centre_feed_lists_the_move_and_insights_state_backend_facts(market_factory):
    market = market_factory()
    app = run_page("overview", market.database, market.now)
    text = " ".join(m.value for m in app.markdown)
    assert "**ANZ** · 1Y" in text and "4.89% → **4.79%**" in text
    assert "ANZ leads the 1Y mortgage market at 4.79%" in text
    assert "1 confirmed mortgage rate move by 1 bank in the last 7 days" in text


def test_highlighting_a_bank_shows_its_gaps_and_a_gap_insight(market_factory):
    market = market_factory()
    app = run_page("overview", market.database, market.now)
    app.selectbox(key="overview_bank").set_value("ASB").run()
    assert not app.exception
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Gap to leader"] == "+10 bps" and metrics["Gap to median"] == "+4 bps"
    assert metrics["Rank"] == "#4 of 5"
    text = " ".join(m.value for m in app.markdown)
    assert "ASB is 10 bps above the 1Y mortgage market leader at 4.89% (rank #4 of 5)." in text


def test_a_quiet_market_shows_a_calm_empty_state_not_zeros(market_factory):
    market = market_factory(anz_new_rate=4.89)
    app = run_page("overview", market.database, market.now)
    assert not app.exception
    text = " ".join(m.value for m in app.markdown)
    assert "No confirmed mortgage repricing in the last 7 days" in text
    assert "No confirmed mortgage repricing events were recorded in the last 7 days." in text


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


def test_a_quarantined_banks_fallback_rate_is_marked_not_shown_as_a_normal_rate(market_factory):
    """Review finding: ANZ's latest reading was rejected; the 4.89% shown is its last confirmed
    rate and must say so (it was previously an unmarked plain rate)."""
    market = market_factory(anz_new_rate=8.49, anz_new_status="warning")
    app = run_page("overview", market.database, market.now)
    matrix = app.dataframe[0].value
    assert matrix.loc["ANZ", "1Y"] == "⚠ 4.89% (quarantined: last confirmed)"
    assert matrix.loc["ASB", "1Y"] == "4.89%"
    assert "8.49" not in " ".join(str(v) for v in matrix.to_numpy().ravel())
