"""Pricing Scenarios: what does the optimiser say under different competitor assumptions?

Decision support only. The optimiser runs when the operator clicks "Run scenario" -- never on a
rerender -- and its result is kept in session state until the inputs change.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from balance_pricing_engine.dashboard.charts import objective_curve
from balance_pricing_engine.dashboard.formatting import (
    fmt_bps,
    fmt_money_m,
    fmt_rate_fraction,
    fmt_term,
)
from balance_pricing_engine.dashboard.models import ScenarioRequest, ScenarioView
from balance_pricing_engine.dashboard.pages import scenario_lab
from balance_pricing_engine.dashboard.pages._common import cached, unwrap
from balance_pricing_engine.dashboard.scenario_text import DISCLAIMER, build_decision_text
from balance_pricing_engine.dashboard.services import DashboardServices

STRATEGIES = ["MARKET_MEDIAN", "MARKET_LEADER", "MARKET_SECOND_LOWEST", "MARKET_MEAN", "MANUAL"]
STATE_KEY = "scenario_result"
# "No change" is the optimiser's answer under an unchanged competitor assumption -- not the
# bank's advertised rate, which the backend never reads.
DISPLAY_LABELS = {"No change": "No-change optimum"}


def _markers(view: ScenarioView) -> dict[str, float]:
    by_label = {row.label: row for row in view.rows}
    markers: dict[str, float] = {}
    if view.request.current_rate_pct is not None:
        markers["Current"] = view.request.current_rate_pct
    if "Match median" in by_label:
        markers["Market median"] = by_label["Match median"].competitor_rate * 100
    if "Match leader" in by_label:
        markers["Market leader"] = by_label["Match leader"].competitor_rate * 100
    if view.model_optimum_rate is not None:
        markers["Model optimum"] = view.model_optimum_rate * 100
    return markers


def _render_result(view: ScenarioView) -> None:
    for exclusion in view.excluded_banks:
        st.warning(
            f"{exclusion.bank} excluded from the benchmark: latest validated observation is "
            f"{exclusion.age_hours:.0f} h old (limit {exclusion.threshold_hours:.0f} h)."
        )
    st.subheader("Scenario comparison")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Scenario": DISPLAY_LABELS.get(row.label, row.label),
                    "Competitor": fmt_rate_fraction(row.competitor_rate),
                    "Our rate": fmt_rate_fraction(row.our_rate),
                    "Objective": fmt_money_m(row.objective),
                }
                for row in view.rows
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "Our rate is the optimiser's recommendation under each competitor assumption; "
        "Objective is the modelled risk-adjusted value. Model optimum is the Match median row."
    )

    st.subheader("Objective by our rate")
    st.altair_chart(objective_curve(view.curve, _markers(view)), width="stretch")

    st.subheader("Selected assumption")
    selected = view.selected
    cols = st.columns(5)
    cols[0].metric("Competitor assumption", fmt_rate_fraction(selected.competitor_rate))
    cols[1].metric("Optimal rate", fmt_rate_fraction(selected.our_rate))
    cols[2].metric("Objective", fmt_money_m(selected.objective))
    cols[3].metric("Expected loan volume", fmt_money_m(selected.expected_loan_volume))
    cols[4].metric("Funding shortfall", fmt_money_m(selected.funding_shortfall))
    st.caption(selected.rationale)
    if view.position is not None:
        pos = view.position
        st.caption(
            f"Current rate vs market: rank #{pos.rank} of {pos.bank_count}, "
            f"{fmt_bps(pos.bps_vs_best)} vs leader, {fmt_bps(pos.bps_vs_median)} vs median."
        )

    st.subheader("Decision-support summary")
    st.markdown(build_decision_text(view))


def _scenarios_tab(services: DashboardServices) -> None:
    overview = unwrap(cached(services, "overview"))
    if overview is None or not overview.matrix.terms:
        return
    terms = list(overview.matrix.terms)
    labels = [fmt_term(t) for t in terms]
    left = st.columns(3)
    term_label = left[0].selectbox(
        "Mortgage term",
        labels,
        index=terms.index(overview.default_term) if overview.default_term in terms else 0,
        key="scenario_term",
    )
    strategy = left[1].selectbox("Competitor benchmark", STRATEGIES, key="scenario_strategy")
    current = left[2].number_input(
        "Your current rate (%)",
        min_value=0.0,
        value=0.0,
        step=0.01,
        format="%.2f",
        key="scenario_current_rate",
        help="Your own advertised rate, used only to position you in the market. "
        "The optimiser does not read it. Leave at 0 if unknown.",
    )
    manual = offset = None
    if strategy == "MANUAL":
        manual = st.number_input(
            "Manual competitor rate (%)",
            min_value=0.0,
            value=5.0,
            step=0.01,
            format="%.2f",
            key="scenario_manual_rate",
        )
    else:
        offset = st.number_input(
            "Offset to benchmark (bps)", value=0.0, step=1.0, key="scenario_offset"
        )
    request = ScenarioRequest(
        term_months=terms[labels.index(term_label)],
        strategy=strategy,
        manual_rate_pct=manual,
        offset_bps=offset or 0.0,
        current_rate_pct=current or None,
    )

    if st.button("Run scenario", type="primary", key="scenario_run"):
        st.session_state[STATE_KEY] = (request, services.scenarios(request))

    stored = st.session_state.get(STATE_KEY)
    if stored is None:
        st.caption("Choose assumptions and click **Run scenario**.")
        return
    stored_request, result = stored
    if stored_request != request:
        st.caption("Inputs changed since this result; click Run scenario to refresh.")
    view = unwrap(result)
    if view is not None:
        _render_result(view)


def render(services: DashboardServices) -> None:
    st.warning(DISCLAIMER)
    scenarios_tab, lab_tab = st.tabs(["Scenarios", "Scenario Lab"])
    with scenarios_tab:
        _scenarios_tab(services)
    with lab_tab:
        scenario_lab.render(services)
