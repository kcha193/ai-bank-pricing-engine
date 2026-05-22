"""Streamlit dashboard for the AI Bank Pricing Engine."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.diagnostics import run_sensitivity_analysis
from balance_pricing_engine.plotting import (
    build_profit_surface_figure,
    build_sensitivity_waterfall_figure,
)
from balance_pricing_engine.reporting import fmt_money, fmt_rate
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

import yaml
from pydantic import ValidationError


_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "configs" / "base.yaml"


def _default_yaml() -> str:
    if _DEFAULT_CONFIG_PATH.exists():
        return _DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    return "# paste your config YAML here\n"


st.set_page_config(page_title="AI Bank Pricing Engine", layout="wide")
st.title("AI Bank Pricing Engine")

with st.sidebar:
    st.header("Config")
    config_yaml = st.text_area(
        "YAML config",
        value=_default_yaml(),
        height=400,
        help="Paste the full EngineConfig YAML. Matches configs/base.yaml format.",
    )
    include_sensitivity = st.checkbox("Include sensitivity analysis", value=False)
    run_button = st.button("Run scenario", type="primary")

if not run_button:
    st.info("Paste a YAML config in the sidebar and click **Run scenario**.")
    st.stop()

try:
    raw = yaml.safe_load(config_yaml)
    EngineConfig(**raw)
except (TypeError, ValidationError, yaml.YAMLError) as exc:
    st.error(f"Config error: {exc}")
    st.stop()


@st.cache_data(show_spinner="Running pricing engine…")
def _run(config_yaml_str: str, include_sensitivity_flag: bool) -> dict:
    cfg = EngineConfig(**yaml.safe_load(config_yaml_str))
    diag = run_baseline_diagnostics(cfg)
    sensitivity_frame = run_sensitivity_analysis(cfg) if include_sensitivity_flag else None
    return {"diagnostics": diag, "sensitivity": sensitivity_frame}


result = _run(config_yaml, include_sensitivity)
diagnostics = result["diagnostics"]
sensitivity = result["sensitivity"]
best = diagnostics.best_result

tab_summary, tab_surface, tab_sensitivity = st.tabs(
    ["Summary", "Profit surface", "Sensitivity"]
)

with tab_summary:
    col1, col2, col3 = st.columns(3)
    col1.metric("Best loan rate", fmt_rate(best.loan_rate))
    col2.metric("Best deposit rate", fmt_rate(best.deposit_rate))
    col3.metric("Objective", fmt_money(best.total_objective))

    col4, col5, col6 = st.columns(3)
    col4.metric("Loan volume", fmt_money(best.expected_loan_volume))
    col5.metric("Deposit volume", fmt_money(best.expected_deposit_volume))
    col6.metric("Funding shortfall", fmt_money(best.funding_shortfall))

    if diagnostics.boundary_diagnostics.has_warning:
        st.warning("Boundary warning — optimizer hit grid edge. Consider expanding the rate grid.")
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
        st.info("Enable **Include sensitivity analysis** in the sidebar and re-run.")
    else:
        fig = build_sensitivity_waterfall_figure(sensitivity)
        if fig is not None:
            st.pyplot(fig)
        st.subheader("Sensitivity data")
        st.dataframe(sensitivity, width="stretch")
