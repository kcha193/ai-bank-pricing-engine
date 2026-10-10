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


def test_direction_filter_hides_non_matching_rows(market_factory):
    market = market_factory()
    app = run_page("rate_changes", market.database, market.now)
    app.selectbox(key="rate_changes_direction").select("UP").run()
    assert not app.exception
    assert any("No rate changes" in i.value for i in app.info)
