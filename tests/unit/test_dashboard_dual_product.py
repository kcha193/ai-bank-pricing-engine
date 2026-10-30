"""Dual-product dashboard: home loans and term deposits are separate, correctly ranked markets.

Fixture (``dual_market_factory``): the same five banks advertise 12M mortgages and 6M/12M term
deposits. Mortgage leader is ANZ at 4.79% (lowest); deposit leader is Westpac at 4.25%
(highest) after its NOW-1d move. ASB also advertises a 4.60% PIE return and a 6.00% managed-fund
return, which must never reach the deposit market.
"""

import pytest
from dashboard_harness import chart_count, frame_with, make_services, run_page

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.dashboard.insights import build_insights
from balance_pricing_engine.dashboard.models import (
    SCENARIO_JOINT,
    SCENARIO_TERM_DEPOSIT,
    ChangeFilters,
    ChangeWindow,
    ScenarioRequest,
)
from balance_pricing_engine.dashboard.products import PRODUCT_ORDER, profile
from balance_pricing_engine.pricing_intelligence.strategy import (
    apply_manual_competitor_rate_to_config,
)

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

HL, TD = ProductType.HOME_LOAN, ProductType.TERM_DEPOSIT


def metrics(app) -> dict:
    return {m.label: m.value for m in app.metric}


def markdown(app) -> str:
    return " ".join(m.value for m in app.markdown)


# ------------------------------------------------------------------- product identity


def test_profiles_state_opposite_ranking_rules_and_never_rely_on_colour():
    loan, deposit = profile(HL), profile(TD)
    assert loan.leader_label == "Mortgage market leader — lowest comparable rate"
    assert deposit.leader_label == "Term deposit market leader — highest comparable rate"
    assert loan.ranking_rule == "Mortgage rates: lower is more attractive to borrowers"
    assert deposit.ranking_rule == "Deposit rates: higher is more attractive to depositors"
    assert "funding cost" in deposit.trade_off and "not the best rate" in deposit.trade_off
    assert loan.accent != deposit.accent and loan.symbol != deposit.symbol
    assert PRODUCT_ORDER == (HL, TD)


# ---------------------------------------------------------------------- services


def test_mortgage_ranking_is_lowest_first_and_deposit_ranking_highest_first(dual_market_factory):
    services = make_services(dual_market_factory())
    loan = services.term_summary(12, HL).value
    deposit = services.term_summary(12, TD).value
    assert (loan.product_type, deposit.product_type) == (HL, TD)
    assert loan.leaders == ("ANZ",) and loan.best_pct == 4.79 == loan.lowest_pct
    assert deposit.leaders == ("Westpac",) and deposit.best_pct == 4.25 == deposit.highest_pct
    assert [r.bank for r in deposit.ranking][:2] == ["Westpac", "Kiwibank"]
    assert loan.median_pct == 4.85 and deposit.median_pct == 4.10
    assert deposit.spread_bps == 25  # 4.00 .. 4.25


def test_deposit_gaps_to_the_leader_are_measured_below_the_highest_rate(dual_market_factory):
    services = make_services(dual_market_factory())
    positions = {p.bank: p for p in services.bank_positions(12, TD).value}
    assert positions["Westpac"].rank == 1 and positions["Westpac"].vs_leader_bps == 0
    assert positions["ANZ"].vs_leader_bps == -25 and positions["ANZ"].rank == 5
    # ASB and BNZ tie at 4.10% and share a competition rank.
    assert positions["ASB"].rank == positions["BNZ"].rank == 3


def test_pie_and_managed_fund_returns_never_enter_the_deposit_market(dual_market_factory):
    services = make_services(dual_market_factory())
    overview = services.overview(TD).value
    shown = {cell.rate_pct for cell in overview.matrix.cells.values()}
    assert 4.60 not in shown and 6.00 not in shown
    assert overview.matrix.cells[("ASB", 12)].rate_pct == 4.10
    assert {cell.rate_category for cell in overview.matrix.cells.values()} == {"term_deposit"}


def test_deposit_terms_reflect_deposit_observations_only(dual_market_factory):
    services = make_services(dual_market_factory())
    assert services.overview(TD).value.selectable_terms == (6, 12)
    assert services.overview(HL).value.selectable_terms == (12,)


def test_mortgage_and_deposit_movements_stay_separate(dual_market_factory):
    services = make_services(dual_market_factory())
    loans = services.changes(ChangeWindow("7d"), ChangeFilters(), HL).value
    deposits = services.changes(ChangeWindow("7d"), ChangeFilters(), TD).value
    assert {(r.bank, r.product_type) for r in loans} == {("ANZ", HL)}
    assert {(r.bank, r.product_type) for r in deposits} == {("Westpac", TD)}
    westpac = next(r for r in deposits if r.term_months == 12)
    assert (westpac.previous_pct, westpac.current_pct, westpac.change_bps) == (3.95, 4.25, 30)
    assert (westpac.previous_rank, westpac.new_rank) == (5, 1)  # deposit ranks: highest first
    # The default stays the mortgage feed for existing callers.
    assert services.changes(ChangeWindow("7d")).value == loans


def test_event_detail_is_looked_up_in_the_events_own_product(dual_market_factory):
    services = make_services(dual_market_factory())
    row = next(
        r for r in services.changes(ChangeWindow("7d"), ChangeFilters(), TD).value
        if r.term_months == 12
    )
    detail = services.event_detail(row.key, TD).value
    assert detail.row.product_type is TD and detail.rate_category == "term_deposit"
    assert detail.market_median_pct == 4.10
    assert services.event_detail(row.key, HL).status == "EMPTY"  # never borrowed across products


def test_history_reconstruction_ranks_each_date_in_the_products_direction(dual_market_factory):
    market = dual_market_factory()
    services = make_services(market)
    deposit = services.history(12, ("Westpac", "Kiwibank"), None, TD).value
    assert deposit.product_type is TD
    first, last = deposit.market[0], deposit.market[-1]
    assert first.leaders == ("Kiwibank",) and first.leader_pct == 4.20  # highest, not lowest
    assert last.leaders == ("Westpac",) and last.leader_pct == 4.25
    assert {p.bank for p in deposit.points} == {"Westpac", "Kiwibank"}
    loan = services.history(12, ("ANZ",), None, HL).value
    assert loan.market[-1].leaders == ("ANZ",) and loan.market[-1].leader_pct == 4.79
    assert all(point.rate_pct > 4.5 for point in loan.points)  # no deposit rate leaks in


def test_header_and_freshness_are_evaluated_per_product(dual_market_factory):
    market = dual_market_factory(stale_deposit_banks=("BNZ",))
    services = make_services(market)
    loan, deposit = services.header(HL).value, services.header(TD).value
    assert (loan.fresh_banks, deposit.fresh_banks) == (5, 4)
    assert loan.last_update != deposit.last_update  # each product's own latest run
    assert {e.bank for e in services.overview(TD).value.exclusions} == {"BNZ"}
    assert services.overview(HL).value.exclusions == ()


def test_quarantine_is_tracked_per_product(quarantine_market_factory):
    services = make_services(quarantine_market_factory(mortgage=False, deposit=True))
    assert services.overview(TD).value.matrix.cells[("ASB", 12)].quarantined
    assert not services.overview(HL).value.matrix.cells[("ANZ", 12)].quarantined


def test_missing_deposit_data_has_its_own_empty_message(market_factory):
    services = make_services(market_factory())
    result = services.overview(TD)
    assert result.status == "EMPTY" and "term-deposit" in result.message
    assert "--product term_deposit" in result.message and "never substituted" in result.message
    assert services.overview(HL).status == "OK"


def test_cached_reads_are_keyed_by_product(dual_market_factory):
    """The page cache must never return a mortgage read for a deposit request."""
    script = """
import streamlit as st
from datetime import datetime
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.dashboard.views._common import cached

settings = load_settings({}, database_path=DB)
services = DashboardServices(settings, clock=lambda: datetime.fromisoformat(NOW))
for product in (ProductType.HOME_LOAN, ProductType.TERM_DEPOSIT, ProductType.HOME_LOAN):
    summary = cached(services, "term_summary", 12, product).value
    st.write(f"{product.value}:{summary.product_type.value}:{','.join(summary.leaders)}")
"""
    market = dual_market_factory()
    script = script.replace("DB", repr(str(market.database))).replace(
        "NOW", repr(market.now.isoformat())
    )
    import streamlit as st

    st.cache_data.clear()
    app = AppTest.from_string(script, default_timeout=30).run()
    assert [m.value for m in app.markdown] == [
        "home_loan:home_loan:ANZ",
        "term_deposit:term_deposit:Westpac",
        "home_loan:home_loan:ANZ",
    ]


def test_insights_name_the_product_and_its_leader_rule(dual_market_factory):
    services = make_services(dual_market_factory())
    lines = build_insights(
        services.overview(TD).value,
        services.term_summary(12, TD).value,
        services.bank_positions(12, TD).value,
        "ANZ",
    )
    assert lines[0] == (
        "ANZ is 25 bps below the 1Y term deposit market leader at 4.00% (rank #5 of 5)."
    )
    assert lines[1].startswith(
        "Westpac leads the 1Y term deposit market at 4.25% (highest comparable rate)"
    )
    assert lines[2].startswith("2 confirmed term deposit rate moves by 1 bank")  # 6M and 12M


# -------------------------------------------------------------------------- pages


def test_command_centre_shows_both_markets_with_their_own_leaders(dual_market_factory):
    market = dual_market_factory()
    app = run_page("overview", market.database, market.now)
    assert not app.exception
    headings = " ".join(h.value for h in app.subheader)
    assert "Home Loan market" in headings and "Term Deposit market" in headings
    values = metrics(app)
    assert values["Mortgage leader"] == "ANZ" and values["Lowest rate"] == "4.79%"
    assert values["Term deposit leader"] == "Westpac" and values["Highest rate"] == "4.25%"
    labels = [m.label for m in app.metric]
    assert labels.count("Market median") >= 2 and labels.count("Repricing · 7d") >= 2
    assert app.button(key="cc_explore_home_loan").label == "Explore home loans"
    assert app.button(key="cc_explore_term_deposit").label == "Explore term deposits"


def test_command_centre_detail_switches_every_dependent_view(dual_market_factory):
    market = dual_market_factory()
    app = run_page("overview", market.database, market.now)
    assert "ANZ leads the 1Y mortgage market at 4.79%" in markdown(app)
    app.segmented_control(key="overview_product").set_value("term_deposit").run()
    assert not app.exception
    text = markdown(app)
    assert "Westpac leads the 1Y term deposit market at 4.25%" in text
    assert "mortgage market at" not in text
    matrix = app.dataframe[0].value
    assert matrix.loc["Westpac", "1Y"] == "◆ 4.25%" and matrix.loc["ANZ", "1Y"] == "4.00%"
    assert list(matrix.columns) == ["6M", "1Y"]
    feed = [m.value for m in app.markdown if m.value.startswith("**")]
    assert any("**Westpac** · 1Y term deposit" in line for line in feed)
    assert not any("**ANZ**" in line for line in feed)


def test_command_centre_without_deposit_data_shows_an_honest_empty_panel(market_factory):
    market = market_factory()
    app = run_page("overview", market.database, market.now)
    assert not app.exception
    text = markdown(app)
    assert "No term deposit market data yet" in text
    assert "--product term_deposit" in " ".join(c.value for c in app.caption)
    assert "Term deposit leader" not in metrics(app)  # no metric borrowed from mortgages
    app.segmented_control(key="overview_product").set_value("term_deposit").run()
    assert not app.exception
    assert any("term-deposit" in i.value for i in app.info)
    assert "4.79%" not in markdown(app)


def test_market_explorer_switches_product_title_ranking_and_labels(dual_market_factory):
    market = dual_market_factory()
    app = run_page("market_watch", market.database, market.now)
    assert any(h.value == "Home Loan Market Explorer" for h in app.header)
    app.segmented_control(key="market_watch_product").set_value("term_deposit").run()
    assert not app.exception
    assert any(h.value == "Term Deposit Market Explorer" for h in app.header)
    ranking = frame_with(app, "Rank")
    assert list(ranking["Bank"])[0] == "Westpac" and list(ranking["Rank"])[0] == 1
    assert "Current deposit rate" in metrics(app)
    subheaders = " ".join(s.value for s in app.subheader)
    assert "Term Deposit ranking at 1Y" in subheaders
    captions = " ".join(c.value for c in app.caption)
    assert "Term deposit market leader — highest comparable rate" in captions
    assert "funding cost" in captions
    assert chart_count(app) == 3


def test_a_term_not_offered_for_the_new_product_resets_safely(dual_market_factory):
    market = dual_market_factory()
    app = run_page("market_watch", market.database, market.now)
    app.segmented_control(key="market_watch_product").set_value("term_deposit").run()
    app.selectbox(key="market_watch_term_term_deposit").set_value("6M").run()
    assert frame_with(app, "Rank")["Rate"].iloc[0] == "4.00%"  # Westpac 6M
    app.segmented_control(key="market_watch_product").set_value("home_loan").run()
    assert not app.exception
    assert app.selectbox(key="market_watch_term").options == ["1Y"]
    assert app.selectbox(key="market_watch_term").value == "1Y"
    assert frame_with(app, "Rank")["Bank"].iloc[0] == "ANZ"


def test_product_choice_follows_the_user_between_pages(dual_market_factory):
    import streamlit as st

    market = dual_market_factory()
    st.cache_data.clear()
    script = f"""
import streamlit as st
from datetime import datetime
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.dashboard.views import market_watch, overview, rate_changes, trends

settings = load_settings({{}}, database_path={str(market.database)!r})
services = DashboardServices(settings, clock=lambda: datetime.fromisoformat({market.now.isoformat()!r}))
pages = {{"overview": overview, "market_watch": market_watch, "trends": trends,
          "rate_changes": rate_changes}}
pages[st.session_state.get("page", "overview")].render(services)
"""
    app = AppTest.from_string(script, default_timeout=30).run()
    app.segmented_control(key="overview_product").set_value("term_deposit").run()
    for page, key in (
        ("market_watch", "market_watch_product"),
        ("trends", "trends_product"),
        ("rate_changes", "rate_changes_product"),
    ):
        app.session_state["page"] = page
        app.run()
        assert not app.exception
        assert app.segmented_control(key=key).value == "term_deposit", page


def test_a_product_query_parameter_opens_a_page_on_that_product(dual_market_factory):
    import streamlit as st

    market = dual_market_factory()
    st.cache_data.clear()
    script = f"""
from datetime import datetime
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.dashboard.views import market_watch
settings = load_settings({{}}, database_path={str(market.database)!r})
market_watch.render(DashboardServices(settings, clock=lambda: datetime.fromisoformat({market.now.isoformat()!r})))
"""
    app = AppTest.from_string(script, default_timeout=30)
    app.query_params["product"] = "term_deposit"
    app.run()
    assert any(h.value == "Term Deposit Market Explorer" for h in app.header)
    bad = AppTest.from_string(script, default_timeout=30)
    bad.query_params["product"] = "crypto"
    bad.run()
    assert not bad.exception
    assert any(h.value == "Home Loan Market Explorer" for h in bad.header)


def test_rate_movements_never_mix_products(dual_market_factory):
    market = dual_market_factory()
    app = run_page("rate_changes", market.database, market.now)
    table = frame_with(app, "Movement")
    assert set(table["Market"]) == {"Home loans"} and set(table["Bank"]) == {"ANZ"}
    app.segmented_control(key="rate_changes_product").set_value("term_deposit").run()
    assert not app.exception
    table = frame_with(app, "Movement")
    assert set(table["Market"]) == {"Term deposits"} and set(table["Bank"]) == {"Westpac"}
    provenance = frame_with(app, "Field").set_index("Field")["Value"]
    assert provenance["Market"] == "Term deposits"
    assert provenance["Rate type"] == "term_deposit"


def test_market_trends_draw_the_deposit_leader_as_the_highest_rate(dual_market_factory):
    market = dual_market_factory()
    app = run_page("trends", market.database, market.now)
    app.segmented_control(key="trends_product").set_value("term_deposit").run()
    assert not app.exception
    assert any(h.value == "Term Deposit Market Trends" for h in app.header)
    subheaders = " ".join(s.value for s in app.subheader)
    assert "Term Deposit rate history · 1Y" in subheaders
    assert "Term Deposit leader and median" in subheaders
    table = frame_with(app, "Days after first move")
    assert set(table["Market"]) == {"Term deposits"} and set(table["Bank"]) == {"Westpac"}


# -------------------------------------------------------------------- pricing studio


def test_manual_deposit_override_sets_only_the_deposit_side(dual_market_factory):
    services = make_services(dual_market_factory())
    import yaml

    base = EngineConfig(**yaml.safe_load(services.settings.engine_config_text))
    updated = apply_manual_competitor_rate_to_config(base, 0.0455, product_type=TD)
    assert updated.market.competitor_deposit_rate == 0.0455
    assert updated.market.competitor_loan_rate == base.market.competitor_loan_rate
    loan = apply_manual_competitor_rate_to_config(base, 0.0509)  # default: loan side, unchanged
    assert loan.market.competitor_loan_rate == 0.0509
    assert loan.market.competitor_deposit_rate == base.market.competitor_deposit_rate


def test_deposit_scenario_reads_the_deposit_market_and_reports_deposit_outputs(
    dual_market_factory,
):
    services = make_services(dual_market_factory())
    import yaml

    base = EngineConfig(**yaml.safe_load(services.settings.engine_config_text))
    request = ScenarioRequest(12, "MARKET_LEADER", None, 0.0, 4.00, mode=SCENARIO_TERM_DEPOSIT)
    view = services.scenarios(request).value
    selected = view.selected
    assert view.mode == SCENARIO_TERM_DEPOSIT and selected.mode == SCENARIO_TERM_DEPOSIT
    assert selected.competitor_rate == pytest.approx(0.0425)  # leader = highest deposit rate
    assert selected.deposit_competitor_rate == pytest.approx(0.0425)
    assert selected.loan_competitor_rate == base.market.competitor_loan_rate  # untouched
    assert selected.our_rate == selected.deposit_rate
    assert selected.expected_deposit_volume is not None
    rows = {r.label: r for r in view.rows}
    assert rows["Match leader"].competitor_rate == pytest.approx(0.0425)
    assert rows["Match median"].competitor_rate == pytest.approx(0.0410)
    assert view.position.rank == 5 and view.position.bps_vs_best == -25
    assert view.deposit_curve and view.excluded_banks == ()


def test_joint_scenario_overlays_both_benchmarks(dual_market_factory):
    services = make_services(dual_market_factory())
    request = ScenarioRequest(
        12,
        "MARKET_MEDIAN",
        None,
        0.0,
        None,
        mode=SCENARIO_JOINT,
        deposit_term_months=6,
        deposit_strategy="MARKET_LEADER",
    )
    view = services.scenarios(request).value
    selected = view.selected
    assert selected.loan_competitor_rate == pytest.approx(0.0485)
    assert selected.deposit_competitor_rate == pytest.approx(0.0400)  # 6M leader, Westpac
    assert view.rows == () and selected.our_rate == selected.loan_rate
    manual = services.scenarios(
        ScenarioRequest(12, "MANUAL", 5.0, 0.0, None, mode=SCENARIO_JOINT, deposit_term_months=6)
    )
    assert manual.status == "ERROR" and "both sides" in manual.message


def test_a_deposit_scenario_without_deposit_data_is_empty_not_a_mortgage_result(market_factory):
    services = make_services(market_factory())
    result = services.scenarios(
        ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None, mode=SCENARIO_TERM_DEPOSIT)
    )
    assert result.status == "EMPTY" and "term-deposit" in result.message


def test_pricing_studio_never_shows_a_mortgage_result_under_a_deposit_heading(
    dual_market_factory,
):
    market = dual_market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.button(key="scenario_run").click().run()
    assert "Optimal home-loan rate" in metrics(app)
    app.segmented_control(key="scenario_mode").set_value(SCENARIO_TERM_DEPOSIT).run()
    assert not app.exception
    labels = metrics(app)
    assert "Optimal home-loan rate" not in labels and "Optimal term-deposit rate" not in labels
    app.button(key="scenario_run_term_deposit").click().run()
    assert not app.exception
    labels = metrics(app)
    assert labels["Competitor deposit assumption"] == "4.10%"  # deposit median
    assert "Optimal term-deposit rate" in labels and "Expected deposit volume" in labels
    assert "Optimal home-loan rate" not in labels and "Expected loan volume" not in labels
    table = frame_with(app, "Scenario")
    assert table.loc[table["Scenario"] == "Match leader", "Competitor"].item() == "4.25%"
    text = markdown(app)
    assert "term-deposit rate" in text and "funding cost" in text
    app.segmented_control(key="scenario_mode").set_value("home_loan").run()
    assert "Optimal home-loan rate" in metrics(app)  # the mortgage result is still its own


def test_pricing_studio_joint_mode_runs_both_sides(dual_market_factory):
    market = dual_market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.segmented_control(key="scenario_mode").set_value(SCENARIO_JOINT).run()
    assert not app.exception
    assert app.selectbox(key="scenario_strategy_joint").options == [
        "Market median",
        "Market leader (lowest rate)",
        "Second-lowest rate",
        "Market mean",
    ]
    assert "Market leader (highest rate)" in app.selectbox(key="scenario_strategy_deposit_joint").options
    app.button(key="scenario_run_joint").click().run()
    assert not app.exception
    labels = metrics(app)
    assert "⌂ Optimal home-loan rate" in labels and "◎ Optimal term-deposit rate" in labels
    assert "◎ Expected deposit volume" in labels and "Funding shortfall" in labels
    assert chart_count(app) == 2  # one objective curve per side, no comparison chart


def test_pricing_studio_opens_on_the_users_product(dual_market_factory):
    import streamlit as st

    market = dual_market_factory()
    st.cache_data.clear()
    script = f"""
import streamlit as st
from datetime import datetime
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.dashboard.views import scenarios
st.session_state.setdefault("pw_shared_product", "term_deposit")
settings = load_settings({{}}, database_path={str(market.database)!r})
scenarios.render(DashboardServices(settings, clock=lambda: datetime.fromisoformat({market.now.isoformat()!r})))
"""
    app = AppTest.from_string(script, default_timeout=30).run()
    assert not app.exception
    assert app.segmented_control(key="scenario_mode").value == SCENARIO_TERM_DEPOSIT


def test_no_page_invokes_an_operational_action_when_switching_products(dual_market_factory):
    from unittest.mock import patch

    from balance_pricing_engine.dashboard import actions

    market = dual_market_factory()
    with patch.object(actions, "run_live") as live, patch.object(actions, "run_dry") as dry:
        for page, key in (
            ("overview", "overview_product"),
            ("market_watch", "market_watch_product"),
            ("rate_changes", "rate_changes_product"),
            ("trends", "trends_product"),
        ):
            app = run_page(page, market.database, market.now)
            app.segmented_control(key=key).set_value("term_deposit").run()
            app.segmented_control(key=key).set_value("home_loan").run()
            assert not app.exception, page
    live.assert_not_called()
    dry.assert_not_called()

