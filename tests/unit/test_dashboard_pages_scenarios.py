from unittest.mock import patch

import pytest

from dashboard_harness import make_services, run_page

import balance_pricing_engine.dashboard.services as services_module
from balance_pricing_engine.dashboard.formatting import fmt_money_m, fmt_rate_fraction
from balance_pricing_engine.dashboard.models import ScenarioRequest
from balance_pricing_engine.pricing_scenario_trigger import run_triggered_scenarios

LABELS = ["No-change optimum", "Match median", "Match leader", "Leader -5bps", "Model optimum"]


def test_page_warns_that_this_is_decision_support_only(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    assert not app.exception
    assert any("Decision support only" in w.value for w in app.warning)


def test_page_does_not_run_the_optimiser_until_the_button_is_clicked(market_factory):
    market = market_factory()
    with patch.object(
        services_module, "run_triggered_scenarios", wraps=run_triggered_scenarios
    ) as optimiser:
        app = run_page("scenarios", market.database, market.now)
        app.run()
        app.run()
        assert optimiser.call_count == 0
        app.button(key="scenario_run").click().run()
        assert optimiser.call_count == 1
        app.run()  # a plain rerender shows the stored result, it does not recompute
        assert optimiser.call_count == 1


def test_displayed_numbers_equal_the_backend_result(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.number_input(key="scenario_current_rate").set_value(4.89)
    app.button(key="scenario_run").click().run()
    assert not app.exception
    table = app.dataframe[0].value
    request = ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, 4.89)
    truth = make_services(market).scenarios(request).value
    assert list(table["Scenario"]) == LABELS
    assert list(table["Our rate"]) == [fmt_rate_fraction(r.our_rate) for r in truth.rows]
    assert list(table["Competitor"]) == [fmt_rate_fraction(r.competitor_rate) for r in truth.rows]
    assert list(table["Objective"]) == [fmt_money_m(r.objective) for r in truth.rows]
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Expected loan volume"] == fmt_money_m(truth.selected.expected_loan_volume)
    assert metrics["Funding shortfall"] == fmt_money_m(truth.selected.funding_shortfall)
    assert metrics["Optimal rate"] == fmt_rate_fraction(truth.selected.our_rate)
    text = " ".join(m.value for m in app.markdown)
    assert "10 bps above the market leader" in text and "Decision support only" in text
    # the scenario comparison dumbbell and the objective curve
    assert sum(len(app.get(kind)) for kind in ("vega_lite_chart", "arrow_vega_lite_chart")) == 2


def test_manual_strategy_needs_and_uses_a_manual_rate(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.selectbox(key="scenario_strategy").select("MANUAL").run()
    app.number_input(key="scenario_manual_rate").set_value(5.09)
    app.button(key="scenario_run").click().run()
    assert not app.exception
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Competitor assumption"] == "5.09%"


def test_a_scenario_error_is_an_operational_message(market_factory):
    market = market_factory()
    with patch.object(
        services_module, "run_baseline_diagnostics", side_effect=RuntimeError("0xBEEF internal")
    ):
        app = run_page("scenarios", market.database, market.now)
        app.button(key="scenario_run").click().run()
    assert not app.exception
    assert any("failed to run" in e.value for e in app.error)
    assert all("0xBEEF" not in e.value for e in app.error)


def test_stale_banks_are_called_out_next_to_the_results(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    app = run_page("scenarios", market.database, market.now)
    app.button(key="scenario_run").click().run()
    assert any("Westpac excluded from the benchmark" in w.value for w in app.warning)


def test_changed_inputs_are_flagged_instead_of_silently_mixing_results(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.button(key="scenario_run").click().run()
    app.selectbox(key="scenario_strategy").select("MARKET_LEADER").run()
    assert any("Inputs changed" in c.value for c in app.caption)


def test_no_data_degrades_to_guidance(tmp_path):
    from datetime import datetime, timezone

    app = run_page("scenarios", tmp_path / "none.db", datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not app.exception
    assert any("No market data" in i.value for i in app.info)


def test_a_result_says_when_it_was_computed(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.button(key="scenario_run").click().run()
    assert any(
        c.value.startswith("Computed ") and "(Pacific/Auckland)" in c.value for c in app.caption
    )


def test_a_result_is_flagged_when_the_market_data_changes_underneath_it(market_factory):
    """Review finding: after a new collection the old result kept rendering, and the 'inputs
    changed' notice did not fire because the inputs were unchanged."""
    import os

    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.button(key="scenario_run").click().run()
    assert not any("Market data has changed" in c.value for c in app.caption)
    stat = market.database.stat()
    os.utime(market.database, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    app.run()
    assert any("Market data has changed since this result" in c.value for c in app.caption)


@pytest.mark.parametrize("changed", ["assumption", "invalid", "missing"])
def test_config_changes_suppress_result_without_recomputing(market_factory, tmp_path, changed):
    import yaml
    from balance_pricing_engine.dashboard.settings import load_settings

    market = market_factory()
    original = load_settings({}).engine_config_text
    path = tmp_path / "engine.yaml"
    path.write_text(original)
    with patch.object(
        services_module, "run_triggered_scenarios", wraps=run_triggered_scenarios
    ) as optimiser:
        app = run_page(
            "scenarios", market.database, market.now, env={"PRICING_WATCH_CONFIG": str(path)}
        )
        app.button(key="scenario_run").click().run()
        assert optimiser.call_count == 1
        assert app.dataframe
        app.run()
        assert app.dataframe and optimiser.call_count == 1
        if changed == "assumption":
            data = yaml.safe_load(original)
            data["response"]["loan_rate_elasticity"] *= 2
            updated = yaml.safe_dump(data)
            path.write_text(updated)
        elif changed == "invalid":
            path.write_text("invalid: [")
        else:
            path.unlink()
        app.run()
        assert not app.exception
        assert any("Engine configuration has changed" in w.value for w in app.warning)
        assert not app.dataframe and not app.metric
        assert optimiser.call_count == 1
        if changed == "assumption":
            app.button(key="scenario_run").click().run()
            assert optimiser.call_count == 2
            assert app.dataframe
            assert not any("Engine configuration has changed" in w.value for w in app.warning)
            assert (
                optimiser.call_args.args[0].response.loan_rate_elasticity
                == data["response"]["loan_rate_elasticity"]
            )
        path.write_text(original)
        app.run()
        assert optimiser.call_count == (2 if changed == "assumption" else 1)
        assert any("Engine configuration has changed" in w.value for w in app.warning) is (
            changed == "assumption"
        )

        if changed != "assumption":
            path.write_text("invalid: [")
            app.button(key="scenario_run").click().run()
            assert any("engine config could not be read" in e.value for e in app.error)
            assert optimiser.call_count == 1


def test_unreadable_config_hides_stored_recommendation(market_factory, tmp_path):
    from pathlib import Path
    from balance_pricing_engine.dashboard.settings import load_settings

    market = market_factory()
    path = tmp_path / "engine.yaml"
    path.write_text(load_settings({}).engine_config_text)
    read_text = Path.read_text

    def read(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("private config location")
        return read_text(candidate, *args, **kwargs)

    with patch.object(
        services_module, "run_triggered_scenarios", wraps=run_triggered_scenarios
    ) as optimiser:
        app = run_page(
            "scenarios", market.database, market.now, env={"PRICING_WATCH_CONFIG": str(path)}
        )
        app.button(key="scenario_run").click().run()
        with patch.object(Path, "read_text", read):
            app.run()
            assert not app.exception
            assert any("Engine configuration has changed" in w.value for w in app.warning)
            assert not app.dataframe and not app.metric
            app.button(key="scenario_run").click().run()
            assert any("engine config could not be read" in e.value for e in app.error)
        assert optimiser.call_count == 1
        assert not any("private config location" in e.value for e in app.error)


def test_legacy_result_without_config_provenance_requires_explicit_run(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.button(key="scenario_run").click().run()
    app.session_state["scenario_result"] = app.session_state["scenario_result"][:4]
    app.run()
    assert not app.dataframe
    assert any("Engine configuration has changed" in w.value for w in app.warning)
