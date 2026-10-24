"""Scenario Lab: the original YAML-driven profit-surface and sensitivity run.

Moved unchanged in behaviour from the former single-script dashboard. It runs the
optimiser only when the operator clicks "Run scenario".
"""

from __future__ import annotations

import streamlit as st
import yaml
from pydantic import ValidationError

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.diagnostics import run_sensitivity_analysis
from balance_pricing_engine.plotting import (
    build_profit_surface_figure,
    build_sensitivity_waterfall_figure,
)
from balance_pricing_engine.reporting import fmt_money, fmt_rate
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


@st.cache_data(show_spinner="Running pricing engine…")
def _run(config_yaml_str: str, include_sensitivity_flag: bool) -> dict:
    cfg = EngineConfig(**yaml.safe_load(config_yaml_str))
    diag = run_baseline_diagnostics(cfg)
    sensitivity_frame = run_sensitivity_analysis(cfg) if include_sensitivity_flag else None
    return {"diagnostics": diag, "sensitivity": sensitivity_frame}


def render(services: DashboardServices) -> None:
    st.subheader("Scenario Lab", anchor=False)
    st.caption(
        "For advanced users: run the optimiser on a full EngineConfig YAML, with the profit "
        "surface and optional sensitivity analysis. Decision support only."
    )
    config_yaml = st.text_area(
        "YAML config",
        value=services.settings.engine_config_text or "# paste your config YAML here\n",
        height=300,
        help="Paste the full EngineConfig YAML. Matches configs/base.yaml format.",
        key="lab_config_yaml",
    )
    include_sensitivity = st.checkbox("Include sensitivity analysis", value=False, key="lab_sens")
    run_button = st.button("Run scenario", type="primary", key="lab_run")

    if not run_button:
        st.info("Paste a YAML config above and click **Run scenario**.")
        return

    try:
        EngineConfig(**yaml.safe_load(config_yaml))
    except (TypeError, ValidationError, yaml.YAMLError):
        st.error("Config error: enter valid YAML matching the EngineConfig schema.")
        return

    try:
        result = _run(config_yaml, include_sensitivity)
    except NotImplementedError:
        st.error(
            "Scenario option error: this configuration does not support the requested analysis."
        )
        return
    diagnostics = result["diagnostics"]
    sensitivity = result["sensitivity"]
    best = diagnostics.best_result

    tab_summary, tab_surface, tab_sensitivity = st.tabs(
        ["Summary", "Profit surface", "Sensitivity"]
    )

    with tab_summary:
        col1, col2, col3 = st.columns(3)
        col1.metric("Best loan rate", fmt_rate(best.loan_rate), border=True)
        col2.metric("Best deposit rate", fmt_rate(best.deposit_rate), border=True)
        col3.metric("Objective", fmt_money(best.total_objective), border=True)

        col4, col5, col6 = st.columns(3)
        col4.metric("Loan volume", fmt_money(best.expected_loan_volume), border=True)
        col5.metric("Deposit volume", fmt_money(best.expected_deposit_volume), border=True)
        col6.metric("Funding shortfall", fmt_money(best.funding_shortfall), border=True)

        if diagnostics.boundary_diagnostics.has_warning:
            st.warning(
                "Boundary warning — optimizer hit grid edge. Consider expanding the rate grid."
            )
        if diagnostics.policy_band_breached:
            st.error("Policy band breached — winning rate is outside the original policy grid.")

        st.subheader("Top results")
        st.dataframe(diagnostics.top_results(10), width="stretch")

    with tab_surface:
        fig = build_profit_surface_figure(diagnostics.grid_results, best)
        if fig is not None:
            st.pyplot(fig)
        else:
            st.info("No grid results available.")

    with tab_sensitivity:
        if sensitivity is None:
            st.info("Enable **Include sensitivity analysis** and re-run.")
        else:
            fig = build_sensitivity_waterfall_figure(sensitivity)
            if fig is not None:
                st.pyplot(fig)
            st.subheader("Sensitivity data")
            st.dataframe(sensitivity, width="stretch")
