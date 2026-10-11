"""The five end-to-end demonstration flows from the task brief, as executable checks.

Each compares what the dashboard shows with direct backend calls on the same data.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from dashboard_harness import make_services, run_page

from balance_pricing_engine.dashboard import actions
from balance_pricing_engine.dashboard.formatting import (
    fmt_money_m,
    fmt_rate_fraction,
    fmt_rate_pct,
)
from balance_pricing_engine.dashboard.models import ChangeWindow, ScenarioRequest
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.pricing_intelligence.config import CollectionConfig
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

ALL_PAGES = (
    "overview",
    "market_watch",
    "rate_changes",
    "trends",
    "scenarios",
    "operations",
    "system_health",
)
BANKS = {"ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"}


def backend(market):
    """Direct backend access to the same stored data the dashboard reads."""
    return PricingIntelligenceService(config=market.config, store=ObservationStore(market.database))


def test_flow_a_market_monitoring_matches_the_backend(market_factory):
    """Overview -> matrix -> select 1Y -> leader, median, spread, ranking."""
    market = market_factory()
    app = run_page("overview", market.database, market.now)
    assert not app.exception
    with backend(market) as service:
        truth = service.get_market_snapshot(now=market.now).term(12)
    matrix = app.dataframe[0].value
    for entry in truth.ranking():  # every matrix cell is the backend's rate
        shown = matrix.loc[entry.bank, "1Y"].removeprefix("◆ ")
        assert shown == fmt_rate_pct(entry.rate_pct)
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Market leader"] == ", ".join(truth.market_leaders)
    assert metrics["Lowest"] == fmt_rate_pct(truth.lowest_rate)
    assert metrics["Median"] == fmt_rate_pct(truth.median_rate)
    assert metrics["Highest"] == fmt_rate_pct(truth.highest_rate)
    assert metrics["Spread"] == f"{truth.spread_bps:.0f} bps"
    ranking = app.dataframe[1].value
    assert [(r.rank, r.bank) for r in truth.ranking()] == list(
        zip(ranking["Rank"], ranking["Bank"])
    )
    assert list(ranking["Rank"]) == [1, 2, 2, 4, 4]  # ties shown exactly as the backend ranks them


def test_flow_b_a_rate_change_flows_through_every_view(market_factory):
    """ANZ 1Y 4.89% -> 4.79%: event, ranking, market summary and history all agree."""
    market = market_factory(anz_new_rate=4.79)
    services = make_services(market)

    event = next(r for r in services.changes(ChangeWindow("7d")).value if r.bank == "ANZ")
    assert (event.previous_pct, event.current_pct, event.change_bps) == (4.89, 4.79, -10.0)
    assert (event.previous_rank, event.new_rank) == (3, 1)

    summary = services.term_summary(12).value
    assert summary.leaders == ("ANZ",) and summary.lowest_pct == 4.79

    history = [p for p in services.history(12, ("ANZ",), None).value.points if p.bank == "ANZ"]
    # the recorded states are 4.89 then 4.79; the line then holds 4.79 up to today
    assert list(dict.fromkeys(p.rate_pct for p in history)) == [4.89, 4.79]
    assert history[-1].rate_pct == 4.79

    app = run_page("rate_changes", market.database, market.now)
    row = app.dataframe[0].value.iloc[0]
    assert (row["Old"], row["New"], row["Movement"]) == ("4.89%", "4.79%", "-10 bps")


def test_flow_c_scenario_decision_support_equals_direct_backend_calls(market_factory):
    """Pricing Scenarios -> Current / Market Median / Market Leader / Model Optimum."""
    from balance_pricing_engine.pricing_scenario_trigger import run_triggered_scenarios

    market = market_factory()
    services = make_services(market)
    app = run_page("scenarios", market.database, market.now)
    app.number_input(key="scenario_current_rate").set_value(4.89)
    app.button(key="scenario_run").click().run()
    assert not app.exception
    table = app.dataframe[0].value

    import yaml

    from balance_pricing_engine.config import EngineConfig

    with backend(market) as service:
        snapshot = service.get_market_snapshot(now=market.now)
    base = EngineConfig(**yaml.safe_load(services.settings.engine_config_text))
    truth = run_triggered_scenarios(base, term_months=12, snapshot=snapshot)
    by_label = {r.label: r for r in truth.rows}
    shown = {row["Scenario"]: row for _, row in table.iterrows()}
    for label, display in (
        ("No change", "No-change optimum"),
        ("Match median", "Match median"),
        ("Match leader", "Match leader"),
        ("Model optimum", "Model optimum"),
    ):
        assert shown[display]["Our rate"] == fmt_rate_fraction(by_label[label].our_rate)
        assert shown[display]["Objective"] == fmt_money_m(by_label[label].objective)
    assert services.scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, 4.89)).value.rows
    assert any("Decision support only" in w.value for w in app.warning)


def test_flow_d_run_once_persist_then_refresh_without_collecting(
    market_factory, fixture_fetcher, monkeypatch
):
    """Run Pricing Watch executes exactly once and persists; Refresh Dashboard never collects."""
    market = market_factory()
    runner = actions.JobRunner(cooldown_seconds=0)
    monkeypatch.setattr(actions, "_DEFAULT_RUNNER", runner)
    collections = []

    def fake_run_live(settings, **kwargs):
        config = settings.intelligence.model_copy(
            update={"collection": CollectionConfig(inter_request_delay_seconds=0)}
        )
        with PricingIntelligenceService(
            config=config,
            store=ObservationStore(settings.intelligence.database_path),
            fetcher=fixture_fetcher(),
        ) as service:
            outcome = service.collect(captured_at_utc=market.now, persist_snapshot=False)
        collections.append(outcome)
        return actions.ActionResult(
            "live",
            "SUCCESS",
            "Pricing Watch run completed.",
            market.now,
            banks=tuple(actions.BankOutcome(b, True) for b in outcome.succeeded_banks),
            summary={
                "rates_accepted": outcome.rate_count,
                "changes_detected": len(outcome.changes),
                "suspicious_changes": 0,
            },
        )

    monkeypatch.setattr(actions, "run_live", fake_run_live)
    app = run_page("operations", market.database, market.now)
    app.checkbox(key="confirm_live").check().run()
    app.button(key="run_live").click().run()
    runner.wait()
    app.run()
    app.run()  # reruns after completion must not relaunch

    assert len(collections) == 1
    result = app.dataframe[0].value
    assert set(result["Bank"]) == BANKS and set(result["Result"]) == {"SUCCESS"}
    store = ObservationStore(market.database)
    assert store.latest_collection_timestamp() == market.now  # observations were persisted
    store.close()
    market_summary = DashboardServices(
        load_settings({}, database_path=str(market.database)), clock=lambda: market.now
    ).term_summary(12)
    with backend(market) as service:
        assert (
            market_summary.value.lowest_pct
            == service.get_market_snapshot(now=market.now).term(12).lowest_rate
        )

    with patch.object(ObservationStore, "record_observations", autospec=True) as writes:
        app.button(key="refresh_dashboard").click().run()
        assert not app.exception
    assert writes.call_count == 0 and len(collections) == 1  # Refresh never scraped or wrote


def test_flow_e_one_collector_failure_degrades_but_never_misleads(market_factory):
    """Westpac fails: others continue, Westpac is stale and degraded, every page still works."""
    market = market_factory(stale_banks=("Westpac",))
    services = make_services(market)

    health = services.health().value
    banks = {b.bank: b for b in health.banks}
    assert banks["Westpac"].freshness.state.value == "STALE"
    assert banks["Westpac"].health.value == "DEGRADED"
    assert all(banks[b].health.value == "HEALTHY" for b in BANKS - {"Westpac"})

    summary = services.term_summary(12).value
    assert "Westpac" not in [r.bank for r in summary.ranking]  # not ranked as current
    assert summary.banks_observed == 4

    overview = services.overview().value
    assert overview.matrix.cells[("Westpac", 12)].stale  # shown, but marked stale
    assert overview.matrix.cells[("ANZ", 12)].rate_pct == 4.79  # the others carried on

    app = run_page("system_health", market.database, market.now)
    warnings = " ".join(w.value for w in app.warning)
    assert "Westpac collection failed" in warnings and "excluded from market statistics" in warnings

    for page in ALL_PAGES:  # the dashboard stays usable
        assert not run_page(page, market.database, market.now).exception, page


@pytest.mark.parametrize("page", ALL_PAGES)
def test_every_page_renders_on_a_missing_database(page, tmp_path):
    app = run_page(page, tmp_path / "none.db", datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not app.exception
    assert not (tmp_path / "none.db").exists()  # viewing a page never creates the store


@pytest.mark.parametrize("page", ALL_PAGES)
def test_every_page_renders_on_a_corrupt_database(page, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("not a SQLite database")
    app = run_page(page, bad, datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not app.exception
