"""Live selectors must not expose the stale-only terms retained in the audit matrix."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dashboard_harness import make_services, run_page

import balance_pricing_engine.dashboard.services as services_module
from balance_pricing_engine.dashboard.models import ScenarioRequest
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

st = pytest.importorskip("streamlit")
AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

PAGES = [
    ("overview", "overview_term"),
    ("market_watch", "market_watch_term"),
    ("trends", "trends_term"),
    ("scenarios", "scenario_term"),
]
CASES = [
    ((24,), (12,), 24, ["2Y"]),
    ((12, 24), (36,), 12, ["1Y", "2Y"]),
    ((6, 24), (12, 36), 6, ["6M", "2Y"]),
    ((), (12, 24), None, []),
    ((), (), None, []),
]


def recorded_market(tmp_path, make_record, live, stale, *, stale_peer=False):
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    database = tmp_path / "terms.db"
    with ObservationStore(database) as store:
        old = now - timedelta(hours=169)
        store.record_observations(
            [make_record(bank="ANZ", term_months=t, observed_at=old) for t in stale]
            + (
                [make_record(bank="BNZ", term_months=24, rate_pct=1.0, observed_at=old)]
                if stale_peer
                else []
            ),
            run_id="old",
            observed_at=old,
        )
        store.record_observations(
            [make_record(bank="ASB", term_months=t, observed_at=now) for t in live],
            run_id="live",
            observed_at=now,
        )
    return SimpleNamespace(database=database, now=now)


@pytest.mark.parametrize("live,stale,default,labels", CASES)
def test_overview_separates_live_terms_from_audit_terms(
    tmp_path, make_record, live, stale, default, labels
):
    market = recorded_market(tmp_path, make_record, live, stale)
    result = make_services(market).overview()
    if not live and not stale:
        assert result.status == "EMPTY"
        return
    overview = result.value
    assert overview.selectable_terms == live
    assert overview.matrix.terms == tuple(sorted(set(live) | set(stale)))
    assert overview.default_term == default
    assert all(overview.matrix.cells[("ANZ", t)].stale for t in stale)
    with pytest.raises(FrozenInstanceError):
        overview.selectable_terms = (99,)


@pytest.mark.parametrize("page,key", PAGES)
@pytest.mark.parametrize("live,stale,default,labels", CASES)
def test_all_pages_offer_only_live_terms_and_preserve_no_data_guidance(
    tmp_path, make_record, page, key, live, stale, default, labels
):
    market = recorded_market(tmp_path, make_record, live, stale)
    with patch.object(services_module, "run_triggered_scenarios") as optimiser:
        app = run_page(page, market.database, market.now)
        assert not app.exception
        selectors = [s for s in app.selectbox if s.key == key]
        if live:
            assert selectors[0].options == labels
            assert selectors[0].value == ("1Y" if default == 12 else labels[0])
            assert not any("No comparable market data" in i.value for i in app.info)
        else:
            assert not selectors
            assert any(("No fresh" if stale else "No market data") in i.value for i in app.info)
            assert not any(b.key == "scenario_run" for b in app.button)
        if page == "overview" and stale:
            matrix = app.dataframe[0].value
            assert all(
                "stale" in matrix.loc["ANZ", label]
                for label in matrix.columns
                if matrix.loc["ANZ", label] != "—"
            )
            assert app.warning
        optimiser.assert_not_called()


def clocked_page(page, market):
    st.cache_data.clear()
    script = f"""
import streamlit as st
from datetime import datetime
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.dashboard.views import {page} as page
settings = load_settings({{}}, database_path={str(market.database)!r})
services = DashboardServices(settings, clock=lambda: st.session_state.get(
    'test_now', datetime.fromisoformat({market.now.isoformat()!r})))
page.render(services)
"""
    return AppTest.from_string(script, default_timeout=30).run()


@pytest.mark.parametrize("page,key", PAGES)
@pytest.mark.parametrize("remaining_live", [True, False])
def test_old_widget_selection_is_removed_after_collection(
    tmp_path, make_record, page, key, remaining_live
):
    market = recorded_market(tmp_path, make_record, (12, 24), ())
    with patch.object(services_module, "run_triggered_scenarios") as optimiser:
        app = clocked_page(page, market)
        assert app.selectbox(key=key).value == "1Y"
        later = market.now + timedelta(hours=169)
        with ObservationStore(market.database) as store:
            store.record_observations(
                [make_record(bank="ASB", term_months=24, observed_at=later)]
                if remaining_live
                else [],
                run_id="new-collection",
                observed_at=later,
            )
        app.session_state["test_now"] = later
        app.run()
        assert not app.exception
        selectors = [s for s in app.selectbox if s.key == key]
        if remaining_live:
            assert selectors[0].options == ["2Y"]
            assert selectors[0].value == "2Y"
        else:
            assert not selectors
            assert any("No fresh" in i.value for i in app.info)
        if page == "overview":
            assert "stale" in app.dataframe[0].value.loc["ASB", "1Y"]
        optimiser.assert_not_called()


@pytest.mark.parametrize("page,key", PAGES)
def test_valid_previous_selection_survives_a_new_live_term(tmp_path, make_record, page, key):
    market = recorded_market(tmp_path, make_record, (12, 24), ())
    app = run_page(page, market.database, market.now)
    app.selectbox(key=key).set_value("2Y").run()
    with ObservationStore(market.database) as store:
        store.record_observations(
            [make_record(bank="ASB", term_months=6, observed_at=market.now)],
            run_id="new-term",
            observed_at=market.now,
        )
    app.run()
    assert not app.exception
    assert app.selectbox(key=key).value == "2Y"


def test_stale_rates_never_reach_rankings_or_scenario_benchmarks(tmp_path, make_record):
    market = recorded_market(tmp_path, make_record, (24,), (12,), stale_peer=True)
    services = make_services(market)
    assert services.term_summary(12).status == "EMPTY"
    assert [r.bank for r in services.term_summary(24).value.ranking] == ["ASB"]
    assert [p.bank for p in services.bank_positions(24).value] == ["ASB"]
    with patch.object(services_module, "run_triggered_scenarios") as optimiser:
        assert (
            services.scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None)).status
            == "EMPTY"
        )
        optimiser.assert_not_called()
    view = services.scenarios(ScenarioRequest(24, "MARKET_MEDIAN", None, 0.0, None)).value
    assert view.selected.competitor_rate == pytest.approx(0.0479)
    assert [(e.bank, e.term_months) for e in view.excluded_banks] == [("BNZ", 24)]
    app = run_page("scenarios", market.database, market.now)
    app.button(key="scenario_run").click().run()
    assert not app.exception
    assert {m.label: m.value for m in app.metric}["Competitor mortgage assumption"] == "4.79%"
    assert any("BNZ excluded" in w.value for w in app.warning)
    assert not any("ANZ excluded" in w.value for w in app.warning)


@pytest.mark.parametrize("remaining_live", [True, False])
def test_scenario_for_a_newly_stale_term_is_hidden_until_explicit_rerun(
    tmp_path, make_record, remaining_live
):
    market = recorded_market(tmp_path, make_record, (12, 24), ())
    app = clocked_page("scenarios", market)
    app.button(key="scenario_run").click().run()
    assert any(m.label == "Optimal home-loan rate" for m in app.metric)
    later = market.now + timedelta(hours=169)
    with ObservationStore(market.database) as store:
        store.record_observations(
            [make_record(bank="ASB", term_months=24, observed_at=later)] if remaining_live else [],
            run_id="new-collection",
            observed_at=later,
        )
    app.session_state["test_now"] = later
    with patch.object(services_module, "run_triggered_scenarios") as optimiser:
        app.run()
        assert not app.exception
        assert not any(m.label == "Optimal home-loan rate" for m in app.metric)
        optimiser.assert_not_called()
    if remaining_live:
        assert any("no longer available" in w.value for w in app.warning)
        app.button(key="scenario_run").click().run()
        assert not app.exception
        assert any(m.label == "Optimal home-loan rate" for m in app.metric)


@pytest.mark.parametrize("fallback_age,expected,default", [(2, (12, 24), 12), (169, (24,), 24)])
def test_selectable_terms_follow_trusted_quarantine_fallback_age(
    tmp_path, make_record, fallback_age, expected, default
):
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    old = now - timedelta(hours=fallback_age)
    database = tmp_path / "fallback.db"
    with ObservationStore(database) as store:
        store.record_observations(
            [make_record(bank="ANZ", observed_at=old)], run_id="confirmed", observed_at=old
        )
        store.record_observations(
            [
                make_record(bank="ANZ", rate_pct=9.0, validation_status="warning", observed_at=now),
                make_record(bank="ASB", term_months=24, observed_at=now),
            ],
            run_id="current",
            observed_at=now,
        )
    overview = make_services(SimpleNamespace(database=database, now=now)).overview().value
    assert overview.selectable_terms == expected
    assert overview.default_term == default
    if fallback_age == 2:
        assert overview.matrix.cells[("ANZ", 12)].quarantined
        assert overview.matrix.cells[("ANZ", 12)].rate_pct == 4.79
    else:
        assert overview.matrix.cells[("ANZ", 12)].stale
