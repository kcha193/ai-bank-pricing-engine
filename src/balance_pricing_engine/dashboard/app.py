"""Streamlit dashboard for the AI Bank Pricing Engine."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import sqlite3

import streamlit as st

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.diagnostics import run_sensitivity_analysis
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.dashboard_data import (
    build_changes_frame,
    build_history_series,
    build_market_grid_frame,
    build_ranking_frame,
    build_term_summary,
    build_freshness_frame,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
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


def _resolve_intelligence_config(config_yaml_str: str) -> PricingIntelligenceConfig:
    """Pull pricing_intelligence settings from the sidebar's EngineConfig YAML.

    Falls back to defaults on any parse/validation failure: this panel
    renders before the scenario gate, so the YAML may be mid-edit or
    otherwise invalid, and a config author's bank selection/freshness/
    validation settings should still take effect when it is valid rather
    than being silently ignored in favour of defaults.
    """
    try:
        engine_config = EngineConfig(**yaml.safe_load(config_yaml_str))
    except Exception:  # noqa: BLE001 - falls back to defaults on any bad YAML
        return PricingIntelligenceConfig()
    return engine_config.pricing_intelligence or PricingIntelligenceConfig()


def _render_market_intelligence() -> None:
    """Render the NZ market-intelligence section.

    Rendered before the scenario gate: the advertised market is worth seeing
    without first running an optimisation. Shows guidance rather than an error
    when no collection has happened yet.
    """
    st.header("NZ market intelligence")

    intelligence_config = _resolve_intelligence_config(config_yaml)
    database_path = Path(intelligence_config.database_path)
    if not database_path.exists():
        st.info(
            "No market data collected yet. Every tracked bank now builds its "
            "rate table client-side, so run `python3 -m balance_pricing_engine."
            "pricing_intelligence collect --render-javascript` (needs the "
            "optional browser extra: `pip install -e '.[browser]' && python3 -m "
            "playwright install chromium`) to populate the observation store."
        )
        return

    with PricingIntelligenceService(
        config=intelligence_config, store=ObservationStore(database_path)
    ) as service:
        snapshot = service.get_market_snapshot()
        last_run = service.store.latest_run_id(product_type=ProductType.HOME_LOAN)
        last_run_at = service.store.get_run_started_at(last_run) if last_run else None
        run_ids_by_bank = service.store.get_current_observation_run_ids(
            product_type=ProductType.HOME_LOAN
        )
        health = build_freshness_frame(
            service.store.get_current_observations(), intelligence_config.selected_banks,
            intelligence_config.max_observation_age_hours, datetime.now(timezone.utc),
            run_ids_by_bank, last_run,
        )
        st.caption(f"Last collection attempt recorded: {last_run_at or 'none'}")
        incomplete = health[health["Status"] != "Fresh"]
        if not incomplete.empty:
            st.warning("Collection freshness needs review: " + ", ".join(incomplete.index))
        if not snapshot.terms:
            st.warning("No fresh comparable mortgage rates are available. Run and verify a collection.")
            st.subheader("Collection freshness")
            st.dataframe(health, width="stretch")
            return

        st.subheader("Current mortgage market (advertised specials)")
        st.dataframe(build_market_grid_frame(snapshot), width="stretch")

        terms = list(snapshot.term_months)
        default_term = 12 if 12 in terms else terms[0]
        selected_term = st.selectbox(
            "Term for market summary", terms, index=terms.index(default_term)
        )

        summary = build_term_summary(snapshot, selected_term)
        if summary:
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Best", f"{summary['best_rate']:.2f}%")
            col2.metric("Median", f"{summary['median_rate']:.2f}%")
            col3.metric("Highest", f"{summary['highest_rate']:.2f}%")
            col4.metric("Spread", f"{summary['spread_bps']:.0f} bps")
            st.caption(
                f"Market leader: {summary['market_leader']} "
                f"across {summary['bank_count']} banks"
            )
            st.dataframe(build_ranking_frame(snapshot, selected_term), width="stretch")

        state = service.get_market_state()
        st.subheader(f"Market state: {state.state.value}")
        st.caption(state.rationale)

        st.subheader("Recent repricing")
        changes_frame = build_changes_frame(
            service.get_changes(days=30, scope_to_market=True)
        )
        if changes_frame.empty:
            st.info("No repricing detected in the last 30 days.")
        else:
            st.dataframe(changes_frame, width="stretch")

        st.subheader("Rate history")
        history_series = build_history_series(
            service.get_rate_history_series(term_months=selected_term, rate_category="special")
        )
        if history_series.empty:
            st.info("Not enough history yet. History builds up as collections run.")
        else:
            st.line_chart(history_series)

        st.subheader("Collection freshness")
        st.dataframe(health, width="stretch")
        st.caption("Last-known rates may survive a failed fetch. Freshness is not source verification.")


try:
    _render_market_intelligence()
except (OSError, sqlite3.Error) as exc:
    st.error(f"Cannot read market data: {exc}")

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


try:
    result = _run(config_yaml, include_sensitivity)
except NotImplementedError as exc:
    st.error(f"Scenario option error: {exc}")
    st.stop()
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
