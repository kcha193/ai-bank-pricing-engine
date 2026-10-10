from unittest.mock import patch

import pytest
import yaml
from dashboard_harness import make_services

import balance_pricing_engine.dashboard.services as services_module
from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.dashboard.models import ScenarioRequest
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_intelligence.strategy import (
    CompetitorRateStrategy,
    apply_manual_competitor_rate_to_config,
    apply_market_strategy_to_config,
)
from balance_pricing_engine.pricing_scenario_trigger import run_triggered_scenarios
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


def base_config(services) -> EngineConfig:
    return EngineConfig(**yaml.safe_load(services.settings.engine_config_text))


def backend_snapshot(market):
    with PricingIntelligenceService(
        config=market.config, store=ObservationStore(market.database)
    ) as service:
        return service.get_market_snapshot(now=market.now)


def test_request_fields_reach_the_backend_unchanged(market_factory):
    market = market_factory()
    services = make_services(market)
    request = ScenarioRequest(12, "MARKET_LEADER", None, -5.0, 4.89)
    with (
        patch.object(
            services_module,
            "apply_market_strategy_to_config",
            wraps=apply_market_strategy_to_config,
        ) as apply_,
        patch.object(
            services_module, "run_triggered_scenarios", wraps=run_triggered_scenarios
        ) as trigger,
    ):
        assert services.scenarios(request).is_ok
    applied = apply_.call_args.kwargs
    assert applied["strategy"] is CompetitorRateStrategy.MARKET_LEADER
    assert applied["term_months"] == 12 and applied["offset_bps"] == -5.0
    assert applied["snapshot"].term(12).market_leaders == ("ANZ",)
    assert trigger.call_args.kwargs["term_months"] == 12


def test_displayed_rows_and_selected_scenario_equal_direct_backend_calls(market_factory):
    market = market_factory()
    services = make_services(market)
    view = services.scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, 4.89)).value
    snapshot = backend_snapshot(market)
    base = base_config(services)

    truth = run_triggered_scenarios(base, term_months=12, snapshot=snapshot)
    assert [
        (r.label, r.strategy, r.competitor_rate, r.our_rate, r.objective) for r in view.rows
    ] == [(r.label, r.strategy, r.competitor_rate, r.our_rate, r.objective) for r in truth.rows]
    assert view.model_optimum_rate == truth.model_optimum.our_rate

    selected_config = apply_market_strategy_to_config(
        base, strategy=CompetitorRateStrategy.MARKET_MEDIAN, snapshot=snapshot, term_months=12
    )
    best = run_baseline_diagnostics(selected_config).best_result
    assert view.selected.our_rate == best.loan_rate
    assert view.selected.objective == best.total_objective
    assert view.selected.expected_loan_volume == best.expected_loan_volume
    assert view.selected.competitor_rate == selected_config.market.competitor_loan_rate
    assert view.curve and max(p.objective for p in view.curve) == best.total_objective


def test_current_rate_is_positioned_with_backend_bps(market_factory):
    market = market_factory()
    view = (
        make_services(market).scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, 4.89)).value
    )
    assert view.position.rank == 4
    assert (view.position.bps_vs_best, view.position.bps_vs_median) == (10.0, 4.0)
    no_current = (
        make_services(market).scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None)).value
    )
    assert no_current.position is None


def test_manual_strategy_uses_the_entered_rate(market_factory):
    market = market_factory()
    services = make_services(market)
    view = services.scenarios(ScenarioRequest(12, "MANUAL", 5.09, 0.0, None)).value
    assert view.selected.competitor_rate == pytest.approx(0.0509)
    manual = apply_manual_competitor_rate_to_config(base_config(services), 0.0509)
    assert view.selected.our_rate == run_baseline_diagnostics(manual).best_result.loan_rate


def test_missing_manual_rate_is_an_operational_error(market_factory):
    result = make_services(market_factory()).scenarios(
        ScenarioRequest(12, "MANUAL", None, 0.0, None)
    )
    assert result.status == "ERROR" and "manual" in result.message.lower()


def test_unknown_term_is_an_empty_result(market_factory):
    result = make_services(market_factory()).scenarios(
        ScenarioRequest(60, "MARKET_MEDIAN", None, 0.0, None)
    )
    assert result.status == "EMPTY" and "60" in result.message


def test_stale_banks_are_reported_as_excluded_from_the_benchmark(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    view = (
        make_services(market).scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None)).value
    )
    assert [e.bank for e in view.excluded_banks] == ["Westpac"]


def test_an_optimiser_failure_never_leaks_internals(market_factory, caplog):
    market = market_factory()
    with patch.object(
        services_module,
        "run_baseline_diagnostics",
        side_effect=RuntimeError("internal boom 0xBEEF"),
    ):
        result = make_services(market).scenarios(
            ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None)
        )
    assert result.status == "ERROR"
    assert "0xBEEF" not in result.message and "Traceback" not in result.message
    assert any(r.exc_info for r in caplog.records)


def test_unsupported_scenario_option_is_reported(market_factory):
    market = market_factory()
    with patch.object(
        services_module,
        "run_baseline_diagnostics",
        side_effect=NotImplementedError("segmented pricing unsupported"),
    ):
        result = make_services(market).scenarios(
            ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None)
        )
    assert result.status == "ERROR" and "segmented pricing unsupported" in result.message


def test_a_missing_database_is_empty(tmp_path):
    from balance_pricing_engine.dashboard.services import DashboardServices
    from balance_pricing_engine.dashboard.settings import load_settings

    services = DashboardServices(load_settings({}, database_path=str(tmp_path / "none.db")))
    assert (
        services.scenarios(ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None)).status == "EMPTY"
    )
