from dashboard_harness import run_page


def test_market_watch_shows_rank_gaps_and_freshness(market_factory):
    market = market_factory()
    app = run_page("market_watch", market.database, market.now)
    assert not app.exception
    frame = app.dataframe[0].value.set_index("Bank")
    assert frame.loc["ASB", "Rank"] == 4
    assert frame.loc["ASB", "vs leader"] == "+10 bps"
    assert frame.loc["ASB", "vs median"] == "+4 bps"
    assert frame.loc["ANZ", "Freshness"] == "FRESH"
    assert frame.loc["ANZ", "Last change"] == "10 Oct 2026 13:00"  # 10 Oct 00:00 UTC = NZDT +13
    assert frame.loc["ASB", "Last change"] == "No recent change"


def test_market_watch_selected_bank_card(market_factory):
    market = market_factory()
    app = run_page("market_watch", market.database, market.now)
    app.selectbox(key="market_watch_bank").set_value("ASB").run()
    assert not app.exception
    values = {m.label: m.value for m in app.metric}
    assert values["Current"] == "4.89%" and values["Rank"] == "#4"
    assert values["vs leader"] == "+10 bps" and values["vs median"] == "+4 bps"


def test_a_quarantined_bank_is_labelled_quarantined_not_unknown(market_factory):
    market = market_factory(anz_new_rate=8.49, anz_new_status="warning")
    app = run_page("market_watch", market.database, market.now)
    frame = app.dataframe[0].value.set_index("Bank")
    assert frame.loc["ANZ", "Freshness"] == "⚠ QUARANTINED (last confirmed rate)"
    assert frame.loc["ASB", "Freshness"] == "FRESH"
    app.selectbox(key="market_watch_bank").set_value("ANZ").run()
    assert {m.label: m.value for m in app.metric}["Freshness"] == "⚠ QUARANTINED"


def test_market_watch_with_no_data_degrades_to_guidance(tmp_path):
    from datetime import datetime, timezone

    app = run_page(
        "market_watch", tmp_path / "none.db", datetime(2026, 10, 11, tzinfo=timezone.utc)
    )
    assert not app.exception
    assert any("No market data" in i.value for i in app.info)
