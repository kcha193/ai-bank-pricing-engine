"""Scenario Lab: the original YAML-driven profit-surface/sensitivity run, now one tab.

Ported from the retired whole-script dashboard tests (stop-until-run, config errors,
summary/surface/sensitivity rendering, unsupported-sensitivity errors).
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import yaml
from dashboard_harness import run_page

from balance_pricing_engine.dashboard.views import scenario_lab

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def test_lab_waits_for_the_operator_to_run(tmp_path):
    app = run_page("scenario_lab", tmp_path / "none.db", NOW)
    assert not app.exception
    assert any("click **Run scenario**" in i.value for i in app.info)
    assert len(app.metric) == 0


def test_lab_reports_config_errors(tmp_path):
    app = run_page("scenario_lab", tmp_path / "none.db", NOW)
    app.text_area(key="lab_config_yaml").set_value("random_seed: [").run()
    app.button(key="lab_run").click().run()
    assert not app.exception
    assert any(e.value.startswith("Config error") for e in app.error)


def test_lab_renders_summary_metrics_and_top_results(tmp_path):
    app = run_page("scenario_lab", tmp_path / "none.db", NOW)
    app.button(key="lab_run").click().run()
    assert not app.exception
    labels = [m.label for m in app.metric]
    assert labels == [
        "Best loan rate",
        "Best deposit rate",
        "Objective",
        "Loan volume",
        "Deposit volume",
        "Funding shortfall",
    ]
    assert len(app.dataframe) == 1  # top results
    assert any("Enable **Include sensitivity analysis**" in i.value for i in app.info)


def test_lab_reports_unsupported_sensitivity_options(tmp_path):
    with patch.object(
        scenario_lab,
        "run_sensitivity_analysis",
        side_effect=NotImplementedError(
            "Sensitivity analysis is not supported for segmented pricing configurations."
        ),
    ):
        app = run_page("scenario_lab", tmp_path / "none.db", NOW)
        app.checkbox(key="lab_sens").check().run()
        app.button(key="lab_run").click().run()
    assert not app.exception
    assert [e.value for e in app.error] == [
        "Scenario option error: this configuration does not support the requested analysis."
    ]


@pytest.mark.parametrize(
    "value",
    ["![secret](https://attacker.invalid/leak)", "[secret](https://attacker.invalid)", "SECRET"],
)
def test_lab_validation_never_displays_yaml_values_or_runs_the_optimiser(tmp_path, value):
    app = run_page("scenario_lab", tmp_path / "none.db", NOW)
    config = yaml.safe_load(app.text_area(key="lab_config_yaml").value)
    config["random_seed"] = value
    app.text_area(key="lab_config_yaml").set_value(yaml.safe_dump(config)).run()
    with patch.object(scenario_lab, "run_baseline_diagnostics") as optimiser:
        app.button(key="lab_run").click().run()
    assert not app.exception
    assert [e.value for e in app.error] == [
        "Config error: enter valid YAML matching the EngineConfig schema."
    ]
    assert not optimiser.called
    assert not app.metric and not app.dataframe


@pytest.mark.parametrize("config", ["secret: [SECRET", "SECRET", "null", "- SECRET"])
def test_lab_malformed_yaml_is_static_and_stops_execution(tmp_path, config):
    app = run_page("scenario_lab", tmp_path / "none.db", NOW)
    app.text_area(key="lab_config_yaml").set_value(config).run()
    with patch.object(scenario_lab, "run_baseline_diagnostics") as optimiser:
        app.button(key="lab_run").click().run()
    assert not app.exception
    assert [e.value for e in app.error] == [
        "Config error: enter valid YAML matching the EngineConfig schema."
    ]
    assert not optimiser.called


def test_lab_unsupported_option_error_does_not_echo_exception(tmp_path):
    with patch.object(
        scenario_lab, "_run", side_effect=NotImplementedError("![SECRET](https://evil.invalid)")
    ):
        app = run_page("scenario_lab", tmp_path / "none.db", NOW)
        app.button(key="lab_run").click().run()
    assert not app.exception
    assert [e.value for e in app.error] == [
        "Scenario option error: this configuration does not support the requested analysis."
    ]
