"""Behaviour introduced by the six-destination redesign: shared filters, insights, the
Operations & Trust composer, legacy URLs, governance notices and scenario differences."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from dashboard_harness import frame_with, make_services, run_page

from balance_pricing_engine.dashboard import actions
from balance_pricing_engine.dashboard.insights import build_insights
from balance_pricing_engine.dashboard.models import ScenarioRequest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def two_pages(market):
    """One session that renders whichever page ``session_state['page']`` names."""
    import streamlit as st

    st.cache_data.clear()
    script = f"""
import streamlit as st
from datetime import datetime
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.dashboard.views import market_watch, overview, trends

settings = load_settings({{}}, database_path={str(market.database)!r})
services = DashboardServices(settings, clock=lambda: datetime.fromisoformat({market.now.isoformat()!r}))
pages = {{"overview": overview, "market_watch": market_watch, "trends": trends}}
pages[st.session_state.get("page", "overview")].render(services)
"""
    return AppTest.from_string(script, default_timeout=30).run()


# --------------------------------------------------------------------------- shared filters


def test_the_highlighted_bank_follows_the_user_to_other_pages(market_factory):
    market = market_factory()
    app = two_pages(market)
    app.selectbox(key="overview_bank").set_value("ASB").run()
    app.session_state["page"] = "market_watch"
    app.run()
    assert not app.exception
    assert app.selectbox(key="market_watch_bank").value == "ASB"
    assert {m.label: m.value for m in app.metric}["Current mortgage rate"] == "4.89%"
    app.session_state["page"] = "trends"
    app.run()
    assert app.selectbox(key="trends_bank").value == "ASB"


def test_a_new_choice_on_one_page_becomes_the_shared_choice(market_factory):
    market = market_factory()
    app = two_pages(market)
    app.session_state["page"] = "market_watch"
    app.run()
    app.selectbox(key="market_watch_bank").set_value("BNZ").run()
    app.session_state["page"] = "overview"
    app.run()
    assert app.selectbox(key="overview_bank").value == "BNZ"


def test_the_command_centre_starts_with_no_bank_highlighted(market_factory):
    market = market_factory()
    app = run_page("overview", market.database, market.now)
    assert app.selectbox(key="overview_bank").value is None
    assert any("Choose a bank to highlight" in c.value for c in app.caption)


def test_a_configured_our_bank_is_the_default_highlight(market_factory, tmp_path):
    import yaml

    from balance_pricing_engine.dashboard.settings import load_settings

    market = market_factory()
    data = yaml.safe_load(load_settings({}).engine_config_text)
    data.setdefault("pricing_intelligence", {})["our_bank"] = "Kiwibank"
    path = tmp_path / "engine.yaml"
    path.write_text(yaml.safe_dump(data))
    app = run_page("overview", market.database, market.now, env={"PRICING_WATCH_CONFIG": str(path)})
    assert not app.exception
    assert app.selectbox(key="overview_bank").value == "Kiwibank"


# --------------------------------------------------------------------------------- insights


def _views(market, bank=None):
    services = make_services(market)
    return (
        services.overview().value,
        services.term_summary(12).value,
        services.bank_positions(12).value,
        bank,
    )


def test_insights_restate_backend_numbers_only(market_factory):
    lines = build_insights(*_views(market_factory(), "ASB"))
    assert lines[0] == "ASB is 10 bps above the 1Y mortgage market leader at 4.89% (rank #4 of 5)."
    assert lines[1] == (
        "ANZ leads the 1Y mortgage market at 4.79% (lowest comparable rate); the median is "
        "4.85% and the spread across 5 banks is 10 bps."
    )
    assert lines[2].startswith("1 confirmed mortgage rate move by 1 bank in the last 7 days")


def test_insights_name_stale_and_quarantined_banks(market_factory):
    stale = build_insights(*_views(market_factory(stale_banks=("Westpac",))))
    assert any(
        "1 bank is excluded from the 1Y mortgage comparison because the latest data is stale: Westpac."
        == line
        for line in stale
    )
    quarantined = build_insights(
        *_views(market_factory(anz_new_rate=8.49, anz_new_status="warning", name="q.db"))
    )
    assert any("The latest 1Y mortgage reading for ANZ was quarantined" in line for line in quarantined)
    assert not any("8.49" in line for line in quarantined)


def test_insights_are_capped_and_never_speculate(market_factory):
    overview, summary, positions, _ = _views(market_factory(stale_banks=("Westpac",)))
    lines = build_insights(overview, summary, positions, "ASB")
    assert 2 <= len(lines) <= 4
    banned = ("because of", "due to", "should", "recommend", "expect")
    assert not any(word in line.lower() for line in lines for word in banned)


# ---------------------------------------------------------------------- operations & trust


def test_operations_and_trust_shows_health_first_and_never_collects_on_render(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    with patch.object(actions, "run_live") as live, patch.object(actions, "run_dry") as dry:
        app = run_page("operations_trust", market.database, market.now)
        app.run()
        app.run()
    assert not app.exception
    assert live.call_count == 0 and dry.call_count == 0
    labels = [t.label for t in app.tabs]
    assert labels[0] == "Data health" and "Collection & maintenance" in labels
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Fresh banks"] == "4 / 5" and metrics["Collectors failing"] == "1"
    assert metrics["Database"] == "HEALTHY"
    assert frame_with(app, "Failures").set_index("Bank").loc["Westpac", "Freshness"] == "⚠ STALE"
    assert app.button(key="run_live").disabled  # still behind the confirmation checkbox


def test_operations_controls_stay_reachable_without_a_database(tmp_path):
    app = run_page("operations_trust", tmp_path / "none.db", NOW)
    assert not app.exception
    assert any(b.key == "refresh_dashboard" for b in app.button)
    assert not (tmp_path / "none.db").exists()


def test_quarantined_readings_are_counted_in_the_health_overview(market_factory):
    market = market_factory(anz_new_rate=8.49, anz_new_status="warning")
    app = run_page("system_health", market.database, market.now)
    assert {m.label: m.value for m in app.metric}["Suspicious readings"] == "1"


# ------------------------------------------------------------------------------ navigation


def test_every_legacy_url_redirects_to_a_registered_destination():
    from balance_pricing_engine.dashboard import app as router

    destinations = {dest for dest, *_ in router.DESTINATIONS}
    paths = {path for _, _, _, path, _ in router.DESTINATIONS}
    assert set(router.LEGACY_PATHS) == {
        "overview",
        "market-watch",
        "rate-changes",
        "trends",
        "scenarios",
        "system-health",
    }
    assert set(router.LEGACY_PATHS.values()) <= destinations
    assert not set(router.LEGACY_PATHS) & paths  # a redirect never shadows a live page
    assert len(destinations) == 6


def test_no_pages_folder_sits_next_to_the_entry_script():
    """A sibling ``pages/`` makes Streamlit start in legacy multipage mode (broken deep links)."""
    from pathlib import Path

    from balance_pricing_engine.dashboard import app as router

    assert not (Path(router.__file__).parent / "pages").exists()


def test_the_full_app_renders_the_command_centre_by_default(market_factory, monkeypatch):
    from pathlib import Path

    market = market_factory()
    monkeypatch.setenv("PRICING_INTELLIGENCE_DB_PATH", str(market.database))
    app_path = Path(__file__).resolve().parents[2] / "src/balance_pricing_engine/dashboard/app.py"
    app = AppTest.from_file(str(app_path), default_timeout=30).run()
    assert not app.exception
    assert any(h.value == "Command Centre" for h in app.header)


# ------------------------------------------------------------------------- pricing studio


def test_governance_flags_from_the_optimiser_are_shown_next_to_results(market_factory):
    import balance_pricing_engine.dashboard.services as services_module

    market = market_factory()
    real = services_module.DashboardServices.scenarios

    def flagged(self, request):
        result = real(self, request)
        view = result.value
        selected = replace(view.selected, boundary_warning=True, policy_band_breached=True)
        return type(result).ok(replace(view, selected=selected))

    with patch.object(services_module.DashboardServices, "scenarios", flagged):
        app = run_page("scenarios", market.database, market.now)
        app.button(key="scenario_run").click().run()
    assert not app.exception
    assert any("Grid-boundary warning" in w.value for w in app.warning)
    assert any("Outside policy band" in w.value for w in app.warning)
    assert not app.error  # a policy-band WARN is not a governance BLOCK


def test_scenario_view_carries_the_optimiser_governance_flags(market_factory):
    view = (
        make_services(market_factory())
        .scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None))
        .value
    )
    assert isinstance(view.selected.boundary_warning, bool)
    assert isinstance(view.selected.policy_band_breached, bool)


def test_scenario_differences_compare_to_match_median_and_say_model_optimum_is_the_same(
    market_factory,
):
    from balance_pricing_engine.dashboard.scenario_text import scenario_differences

    view = (
        make_services(market_factory())
        .scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None))
        .value
    )
    rows = {r["Scenario"]: r for r in scenario_differences(view)}
    median = rows["Match median — Median-benchmark optimum (reference)"]
    assert median["Objective vs Match median"] == "same"
    assert len(rows) == 4  # median reference is not a fifth strategy
    assert "No-change assumption" in rows


def test_pricing_studio_explains_that_the_model_optimum_is_an_alias(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.button(key="scenario_run").click().run()
    text = " ".join(c.value for c in app.caption) + " ".join(m.value for m in app.markdown)
    assert (
        "Median-benchmark optimum (reference) is the Match median row, not a separate strategy"
        in text
    )
    assert "not your bank's advertised rate" in text


def test_stale_exclusions_are_grouped_into_one_sentence_per_bank():
    from balance_pricing_engine.dashboard.views._common import exclusion_messages
    from balance_pricing_engine.pricing_intelligence.health import MarketExclusion

    def stale(bank, term, age):
        return MarketExclusion(
            bank=bank,
            term_months=term,
            product_name="Fixed Special",
            rate_pct=5.0,
            observed_at=NOW,
            age_hours=age,
            threshold_hours=168.0,
        )

    messages = exclusion_messages(
        [stale("Westpac", 24, 242.2), stale("Westpac", 12, 241.8), stale("BNZ", 12, 200.0)]
    )
    assert messages == [
        "Westpac excluded at 1Y, 2Y: latest validated observation is 242 h old (limit 168 h).",
        "BNZ excluded at 1Y: latest validated observation is 200 h old (limit 168 h).",
    ]
