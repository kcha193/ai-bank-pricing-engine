from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest


class DashboardStop(Exception):
    pass


class FakeContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeStreamlit(FakeContext):
    def __init__(self, *, run_button: bool, include_sensitivity: bool, config_yaml: str):
        self.run_button = run_button
        self.include_sensitivity = include_sensitivity
        self.config_yaml = config_yaml
        self.sidebar = FakeContext()
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def set_page_config(self, **kwargs):
        self._record("set_page_config", **kwargs)

    def title(self, *args, **kwargs):
        self._record("title", *args, **kwargs)

    def header(self, *args, **kwargs):
        self._record("header", *args, **kwargs)

    def text_area(self, *args, **kwargs):
        self._record("text_area", *args, **kwargs)
        return self.config_yaml

    def checkbox(self, *args, **kwargs):
        self._record("checkbox", *args, **kwargs)
        return self.include_sensitivity

    def button(self, *args, **kwargs):
        self._record("button", *args, **kwargs)
        return self.run_button

    def info(self, *args, **kwargs):
        self._record("info", *args, **kwargs)

    def error(self, *args, **kwargs):
        self._record("error", *args, **kwargs)

    def warning(self, *args, **kwargs):
        self._record("warning", *args, **kwargs)

    def subheader(self, *args, **kwargs):
        self._record("subheader", *args, **kwargs)

    def dataframe(self, *args, **kwargs):
        self._record("dataframe", *args, **kwargs)

    def pyplot(self, *args, **kwargs):
        self._record("pyplot", *args, **kwargs)

    def metric(self, *args, **kwargs):
        self._record("metric", *args, **kwargs)

    def columns(self, count: int):
        self._record("columns", count)
        return [self for _ in range(count)]

    def tabs(self, labels):
        self._record("tabs", labels)
        return [FakeContext() for _ in labels]

    def cache_data(self, **kwargs):
        self._record("cache_data", **kwargs)

        def decorator(func):
            return func

        return decorator

    def stop(self):
        self._record("stop")
        raise DashboardStop


def _fresh_dashboard_import(monkeypatch, fake_streamlit: FakeStreamlit):
    monkeypatch.setitem(sys.modules, "streamlit", fake_streamlit)
    sys.modules.pop("balance_pricing_engine.dashboard.app", None)
    return importlib.import_module("balance_pricing_engine.dashboard.app")


def _fake_diagnostics(*, boundary_warning: bool = True, policy_breach: bool = True):
    best = SimpleNamespace(
        loan_rate=0.081,
        deposit_rate=0.0495,
        total_objective=12_300_000.0,
        expected_loan_volume=110_000_000.0,
        expected_deposit_volume=98_000_000.0,
        funding_shortfall=12_000_000.0,
    )
    return SimpleNamespace(
        best_result=best,
        boundary_diagnostics=SimpleNamespace(has_warning=boundary_warning),
        policy_band_breached=policy_breach,
        grid_results=pd.DataFrame({"loan_rate": [0.081], "deposit_rate": [0.0495]}),
        top_results=lambda count: pd.DataFrame({"rank": [1], "count": [count]}),
    )


def test_dashboard_stops_until_user_runs_scenario(monkeypatch):
    fake_st = FakeStreamlit(run_button=False, include_sensitivity=False, config_yaml="")

    with pytest.raises(DashboardStop):
        _fresh_dashboard_import(monkeypatch, fake_st)

    assert any(call[0] == "info" for call in fake_st.calls)
    assert any(call[0] == "stop" for call in fake_st.calls)


def test_dashboard_reports_config_errors(monkeypatch):
    fake_st = FakeStreamlit(run_button=True, include_sensitivity=False, config_yaml="random_seed: [")

    with pytest.raises(DashboardStop):
        _fresh_dashboard_import(monkeypatch, fake_st)

    assert any(call[0] == "error" for call in fake_st.calls)


def test_dashboard_renders_summary_surface_and_sensitivity(monkeypatch):
    import balance_pricing_engine.diagnostics as diagnostics_module
    import balance_pricing_engine.plotting as plotting_module
    import balance_pricing_engine.scenarios.runner as runner_module

    fake_st = FakeStreamlit(
        run_button=True,
        include_sensitivity=True,
        config_yaml=Path("configs/base.yaml").read_text(encoding="utf-8"),
    )
    monkeypatch.setattr(
        runner_module,
        "run_baseline_diagnostics",
        lambda config: _fake_diagnostics(),
    )
    monkeypatch.setattr(
        diagnostics_module,
        "run_sensitivity_analysis",
        lambda config: pd.DataFrame(
            {"scenario_name": ["baseline", "stress"], "delta_objective_pct": [0.0, -2.5]}
        ),
    )
    monkeypatch.setattr(plotting_module, "build_profit_surface_figure", lambda grid, best: "surface")
    monkeypatch.setattr(
        plotting_module,
        "build_sensitivity_waterfall_figure",
        lambda sensitivity: "waterfall",
    )

    _fresh_dashboard_import(monkeypatch, fake_st)

    call_names = [call[0] for call in fake_st.calls]
    assert call_names.count("metric") == 6
    assert call_names.count("pyplot") == 2
    assert "warning" in call_names
    assert "dataframe" in call_names
