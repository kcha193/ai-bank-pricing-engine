import pytest

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
    assert list(app.dataframe[0].value["Rank"]) == [1, 2, 2, 4, 4]  # ties exactly as ranked


def test_market_explorer_tabs_hold_ranking_gaps_matrix_and_term_structure(market_factory):
    from dashboard_harness import chart_count

    market = market_factory()
    app = run_page("market_watch", market.database, market.now)
    assert [t.label for t in app.tabs] == [
        "Ranking",
        "Gap analysis",
        "Bank × term matrix",
        "Term structure",
    ]
    assert chart_count(app) == 3  # ranking, gaps, term structure
    matrix = [e.value for e in app.dataframe if "1Y" in e.value.columns]
    assert matrix[0].loc["ANZ", "1Y"] == "◆ 4.79%"
    app.multiselect(key="explorer_structure_banks").set_value([]).run()
    assert not app.exception and chart_count(app) == 2


def test_the_selected_bank_detail_shows_its_terms_and_recent_moves(market_factory):
    market = market_factory()
    app = run_page("market_watch", market.database, market.now)
    app.selectbox(key="market_watch_bank").set_value("ANZ").run()
    assert any("ANZ in detail" in s.value for s in app.subheader)
    text = " ".join(m.value for m in app.markdown)
    assert "**ANZ** · 1Y" in text and "4.89% → **4.79%**" in text


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


def test_market_watch_does_not_label_a_fresh_term_stale_due_to_another_term(tmp_path, make_record):
    from datetime import datetime, timedelta, timezone
    from balance_pricing_engine.pricing_intelligence.store import ObservationStore

    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    database = tmp_path / "mixed.db"
    store = ObservationStore(database)
    old = now - timedelta(days=10)
    store.record_observations(
        [make_record(bank="ASB", term_months=36, observed_at=old)],
        run_id="old-other-term",
        observed_at=old,
    )
    store.record_observations(
        [make_record(bank="ASB", observed_at=now)], run_id="selected-term", observed_at=now
    )
    store.close()
    app = run_page("market_watch", database, now)
    assert not app.exception
    assert app.dataframe[0].value.set_index("Bank").loc["ASB", "Freshness"] == "FRESH"


@pytest.mark.parametrize(
    "healthy_pct,quarantined,name",
    [
        (4.0, False, "Healthy sibling"),
        (5.0, True, "Healthy sibling"),
        (5.0, False, "Fixed Special"),
    ],
)
def test_both_pages_badge_the_selected_offer_not_a_sibling(
    market_factory, make_record, healthy_pct, quarantined, name
):
    from datetime import timedelta
    from balance_pricing_engine.pricing_intelligence.store import ObservationStore

    market = market_factory(anz_new_rate=8.49, anz_new_status="warning")
    when = market.now - timedelta(hours=12)
    with ObservationStore(market.database) as store:
        warning = next(r for r in store.get_current_observations() if r.bank == "ANZ")
        sibling = make_record(
            bank="ANZ", product_name=name, rate_pct=healthy_pct, max_lvr=0.8, observed_at=when
        )
        store.record_observations([sibling, warning], run_id="sibling", observed_at=when)
    watch = run_page("market_watch", market.database, market.now)
    assert not watch.exception
    label = watch.dataframe[0].value.set_index("Bank").loc["ANZ", "Freshness"]
    assert ("QUARANTINED" in label) is quarantined
    overview = run_page("overview", market.database, market.now)
    assert not overview.exception
    label = overview.dataframe[0].value.loc["ANZ", "1Y"]
    assert ("quarantined: last confirmed" in label) is quarantined
