"""End-to-end demonstration of the v0.2.0 operational pipeline, in one file.

Walks the full path the operations doc promises:

    scheduled/manual run
        -> collect five banks (fixture pages, no live site)
        -> validate observations
        -> persist
        -> detect a meaningful change
        -> calculate market snapshot / market state
        -> auto-trigger pricing scenarios
        -> format a Telegram alert (enriched with the scenario block)
        -> persist the operational run result (RunRecord + scenario_runs)

Plus two negative demonstrations: a no-change run produces no alert and no
scenario trigger, and a partial collector failure degrades gracefully
(PARTIAL_SUCCESS, other banks unaffected, stale data not silently read as
current) rather than failing the whole pipeline.

Everything here runs against fixture HTML (via the ``fixture_fetcher``
conftest fixture) and a small/fast EngineConfig -- no live bank site, no real
Telegram/webhook call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from balance_pricing_engine.competitor_rates.collectors import COLLECTOR_CLASSES
from balance_pricing_engine.config import load_config
from balance_pricing_engine.pricing_intelligence.alerts import CollectingAlertSink
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.orchestration import ScenarioResult, run_operational_cycle
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_scenario_trigger import (
    format_scenario_block,
    run_triggered_scenarios,
)

DAY_ONE = datetime.now(timezone.utc) - timedelta(hours=2)
DAY_TWO = datetime.now(timezone.utc) - timedelta(hours=1)


def _small_engine_config():
    config = load_config("configs/base.yaml")
    simulation = config.simulation.model_copy(
        update={"n_loan_customers": 300, "n_deposit_customers": 300}
    )
    return config.model_copy(update={"simulation": simulation})


def _mutate_anz_one_year(bank, html):
    """ANZ's 1-year special reprices -20 bps: 4.79% -> 4.59%."""
    if bank == "ANZ":
        return html.replace("<td>1 year</td><td>4.79%", "<td>1 year</td><td>4.59%")
    return html


def _scenario_trigger_fn(pricing_config):
    """Mirrors exactly what scripts/collect_market_rates_workflow.py builds
    when --config is supplied -- the real (not faked) scenario computation."""

    def trigger(terms, snapshot):
        results = {}
        for term in terms:
            comparison = run_triggered_scenarios(pricing_config, term_months=term, snapshot=snapshot)
            results[term] = ScenarioResult(
                data=comparison.as_dicts(), alert_block=format_scenario_block(comparison)
            )
        return results

    return trigger


def _config(tmp_path):
    return PricingIntelligenceConfig(
        database_path=str(tmp_path / "observations.db"),
        collection=CollectionConfig(inter_request_delay_seconds=0),
    )


def test_a_meaningful_reprice_flows_through_the_full_operational_pipeline(
    tmp_path, fixture_fetcher
):
    """ANZ 1Y 4.79% -> 4.59% (-20 bps) end to end."""
    config = _config(tmp_path)
    pricing_config = _small_engine_config()
    sink = CollectingAlertSink()

    # -- bootstrap collection: five banks, fixture pages, no live site -----
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        baseline = run_operational_cycle(service, captured_at_utc=DAY_ONE, alert_sinks={})

    assert baseline.status == "SUCCESS"
    assert set(baseline.run_record.banks_successful) == {c.bank for c in COLLECTOR_CLASSES}
    assert baseline.outcome.changes == ()  # nothing to compare against yet

    # -- the reprice run: collect -> validate -> persist -> detect ---------
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        fetcher=fixture_fetcher(mutate=_mutate_anz_one_year),
    ) as service:
        result = run_operational_cycle(
            service,
            captured_at_utc=DAY_TWO,
            alert_sinks={"telegram": sink},
            scenario_trigger_fn=_scenario_trigger_fn(pricing_config),
        )

        # -- market snapshot / median reflect the new rate -----------------
        snapshot = service.get_market_snapshot()
        one_year = snapshot.term(12)
        assert one_year.rate_for("ANZ") == 4.59
        assert one_year.market_leader == "ANZ"  # new rank: #1

    # -- change detected -----------------------------------------------------
    assert len(result.outcome.changes) == 1
    change = result.outcome.changes[0]
    assert change.bank == "ANZ"
    assert change.change_bps == -20.0
    assert change.suspicious is False

    # -- scenario auto-triggered for the affected term (1Y = 12 months) -----
    with ObservationStore(config.database_path) as store:
        scenario_runs = store.get_scenario_runs(result.run_record.run_id)
    assert len(scenario_runs) == 1
    assert scenario_runs[0]["term_months"] == 12
    labels = [row["label"] for row in scenario_runs[0]["scenarios"]]
    assert labels == ["No change", "Match median", "Match leader", "Leader -5bps", "Model optimum"]

    # -- Telegram alert formatted with the scenario enrichment --------------
    assert len(sink.alerts) == 1
    alert = sink.alerts[0]
    assert "NZ MORTGAGE RATE CHANGE" in alert.title
    assert "ANZ" in alert.body
    assert "4.79% → 4.59%" in alert.body
    assert "PRICING ENGINE" in alert.body
    assert "Decision support only -- no automatic pricing action taken." in alert.body

    # -- operational run result persisted ------------------------------------
    assert result.status == "SUCCESS"
    assert result.run_record.rates_changed == 1
    assert result.run_record.market_state is not None
    with ObservationStore(config.database_path) as store:
        persisted = store.get_run_record(result.run_record.run_id)
    assert persisted == result.run_record


def test_no_market_change_triggers_no_alert_and_no_scenario_run(tmp_path, fixture_fetcher):
    """A quiet day: identical fixture data collected twice produces no
    unnecessary alert and no unnecessary scenario run."""
    config = _config(tmp_path)
    pricing_config = _small_engine_config()
    sink = CollectingAlertSink()

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        run_operational_cycle(service, captured_at_utc=DAY_ONE, alert_sinks={})

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        result = run_operational_cycle(
            service,
            captured_at_utc=DAY_TWO,
            alert_sinks={"telegram": sink},
            scenario_trigger_fn=_scenario_trigger_fn(pricing_config),
        )

    assert result.outcome.changes == ()
    assert sink.alerts == []
    assert result.alerts_sent == 0
    with ObservationStore(config.database_path) as store:
        assert store.get_recent_scenario_runs() == []


def test_a_collector_failure_does_not_take_down_the_whole_pipeline(tmp_path, fixture_fetcher):
    """One bank failing degrades gracefully: PARTIAL_SUCCESS, the other four
    banks persist normally, and Westpac's now-stale rate is flagged rather
    than silently read as a fresh, current observation."""
    config = _config(tmp_path)
    sink = CollectingAlertSink()

    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        run_operational_cycle(service, captured_at_utc=DAY_ONE, alert_sinks={})

    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path),
        fetcher=fixture_fetcher(failing_banks=["Westpac"]),
    ) as service:
        result = run_operational_cycle(
            service, captured_at_utc=DAY_TWO, alert_sinks={"telegram": sink}
        )
        health = {h.bank: h for h in service.store.get_collector_health()}
        current = {r.bank for r in service.store.get_current_observations()}

    assert result.status == "PARTIAL_SUCCESS"
    assert result.code == 2
    assert "Westpac" in result.run_record.banks_failed
    assert {"ANZ", "ASB", "BNZ", "Kiwibank"} <= set(result.run_record.banks_successful)

    # Westpac's prior observation survives the failed fetch (last-known, not
    # discarded) but its collector-health streak reflects the failure --
    # "unknown, not withdrawn" for change-detection, and separately visible
    # as a degraded collector for operational monitoring.
    assert "Westpac" in current
    assert health["Westpac"].consecutive_failures == 1
    assert health["ANZ"].consecutive_failures == 0

    # A partial failure is itself not a "meaningful change" -- no alert noise
    # from the failure alone (no rate moved).
    assert sink.alerts == []


def _load_workflow_script():
    import importlib.util
    from pathlib import Path

    path = Path("scripts/collect_market_rates_workflow.py")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_run_summary_is_json_serialisable_for_a_scheduler(
    tmp_path, fixture_fetcher, monkeypatch
):
    """Sanity check the artifact an operator actually reads: run_collection()
    (the scheduled entry point) produces a plain-JSON summary."""
    workflow = _load_workflow_script()

    config = _config(tmp_path)
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), fetcher=fixture_fetcher()
    ) as service:
        run_operational_cycle(service, captured_at_utc=DAY_ONE, alert_sinks={})

    monkeypatch.setattr(
        workflow,
        "PricingIntelligenceService",
        lambda **kwargs: PricingIntelligenceService(
            config=kwargs["config"], store=kwargs["store"], fetcher=fixture_fetcher()
        ),
    )

    code, summary = workflow.run_collection(
        config, render_javascript=False, product_type="home_loan"
    )
    # Re-collecting identical fixture data a second time: a quiet run.
    assert code == workflow.EXIT_OK
    json.dumps(summary)  # must not raise
