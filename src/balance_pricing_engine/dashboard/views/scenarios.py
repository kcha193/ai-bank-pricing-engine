"""Pricing Studio: what does the optimiser say under different competitor assumptions?

Decision support only. Three steps -- define, run & compare, understand the trade-offs -- with
the original YAML Scenario Lab one tab away for advanced use. The optimiser runs when the
operator clicks "Run scenario" -- never on a rerender -- and its result is kept in session state
with the inputs, the database stamp and the engine-config fingerprint it was computed from.
A changed config or an unavailable term hides the result until an explicit rerun.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from balance_pricing_engine.dashboard import ui
from balance_pricing_engine.dashboard.charts import objective_curve, scenario_rates
from balance_pricing_engine.dashboard.formatting import (
    fmt_bps,
    fmt_money_m,
    fmt_nz_datetime,
    fmt_rate_fraction,
    fmt_term,
)
from balance_pricing_engine.dashboard.models import ScenarioRequest, ScenarioView
from balance_pricing_engine.dashboard.views import scenario_lab
from balance_pricing_engine.dashboard.views._common import cached, live_term_selector, unwrap
from balance_pricing_engine.dashboard.scenario_text import (
    DISCLAIMER,
    DISPLAY_LABELS,
    build_decision_text,
    scenario_differences,
)
from balance_pricing_engine.dashboard.services import DashboardServices

STRATEGIES = ["MARKET_MEDIAN", "MARKET_LEADER", "MARKET_SECOND_LOWEST", "MARKET_MEAN", "MANUAL"]
STRATEGY_LABELS = {
    "MARKET_MEDIAN": "Market median",
    "MARKET_LEADER": "Market leader (lowest rate)",
    "MARKET_SECOND_LOWEST": "Second-lowest rate",
    "MARKET_MEAN": "Market mean",
    "MANUAL": "Manual competitor rate",
}
STATE_KEY = "scenario_result"

INPUT_GUIDE = """
- **Mortgage term** — which comparable advertised special the competitor benchmark is read from.
  Only terms with fresh market data are offered.
- **Competitor benchmark** — how the competitor rate fed into the optimiser is derived from the
  live market: its median, its leader, the second-lowest rate or the mean; or a rate you type in.
- **Offset to benchmark** — shifts the derived competitor rate, in basis points (+10 = 0.10%
  higher), to test "what if competitors move".
- **Your current advertised rate** — optional. It only positions you against the market (rank and
  gaps). The optimiser never reads it, and it is never confused with the model's output.
"""

LIMITATIONS = """
- The response model is calibrated on **synthetic** customers; the objective is a modelled,
  risk-adjusted value, not a forecast of profit.
- Pricing is **portfolio-level** (one home-loan rate), not personalised.
- **No-change optimum** is the optimiser's answer with the competitor assumption unchanged — it is
  not your bank's advertised rate.
- **Model optimum** is the same optimiser run as **Match median** (the model's reference
  benchmark), shown for orientation; it is not a separately calculated strategy.
- Sensitivity analysis and the full profit surface are in **Advanced: Scenario Lab**.
- Every output requires human review. Nothing here changes a price.
"""


def _markers(view: ScenarioView) -> dict[str, float]:
    by_label = {row.label: row for row in view.rows}
    markers: dict[str, float] = {}
    if view.request.current_rate_pct is not None:
        markers["Current (entered)"] = view.request.current_rate_pct
    if "Match median" in by_label:
        markers["Market median"] = by_label["Match median"].competitor_rate * 100
    if "Match leader" in by_label:
        markers["Market leader"] = by_label["Match leader"].competitor_rate * 100
    if view.model_optimum_rate is not None:
        markers["Model optimum"] = view.model_optimum_rate * 100
    return markers


def _governance(view: ScenarioView) -> None:
    for exclusion in view.excluded_banks:
        st.warning(
            f"{exclusion.bank} excluded from the benchmark: latest validated observation is "
            f"{exclusion.age_hours:.0f} h old (limit {exclusion.threshold_hours:.0f} h)."
        )
    if view.selected.boundary_warning:
        st.warning(
            "Governance: the optimiser's best rate sits on the edge of its rate grid. The true "
            "optimum may lie outside the searched range; review before relying on it."
        )
    if view.selected.policy_band_breached:
        st.error(
            "Governance: the winning rate is outside the original policy band. "
            "It requires explicit review."
        )


def _render_result(view: ScenarioView) -> None:
    _governance(view)
    selected = view.selected

    ui.section(
        "2 · Results",
        f"Selected assumption: {selected.rationale}. Rates below are modelled outputs.",
    )
    cols = st.columns(5)
    cols[0].metric(
        "Optimal rate",
        fmt_rate_fraction(selected.our_rate),
        border=True,
        help="The optimiser's best home-loan rate under the selected competitor assumption.",
    )
    cols[1].metric(
        "Competitor assumption",
        fmt_rate_fraction(selected.competitor_rate),
        border=True,
        help="The competitor rate the optimiser was given (benchmark plus any offset).",
    )
    cols[2].metric(
        "Objective",
        fmt_money_m(selected.objective),
        border=True,
        help="Modelled expected risk-adjusted value after the funding-shortfall penalty.",
    )
    cols[3].metric("Expected loan volume", fmt_money_m(selected.expected_loan_volume), border=True)
    cols[4].metric("Funding shortfall", fmt_money_m(selected.funding_shortfall), border=True)
    if view.position is not None:
        pos = view.position
        st.caption(
            f"Your entered current rate vs market: rank #{pos.rank} of {pos.bank_count}, "
            f"{fmt_bps(pos.bps_vs_best)} vs leader, {fmt_bps(pos.bps_vs_median)} vs median."
        )

    ui.section(
        "Scenario comparison",
        "Each scenario re-runs the optimiser under a different competitor assumption.",
    )
    chart, table = st.columns([3, 2], gap="large")
    chart.altair_chart(scenario_rates(view.rows, DISPLAY_LABELS), width="stretch")
    table.dataframe(
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
    table.caption(
        "Our rate is the optimiser's recommendation under each competitor assumption; "
        "Objective is the modelled risk-adjusted value. Model optimum is the Match median row, "
        "not a separate strategy."
    )

    ui.section(
        "3 · Understand the trade-offs",
        "The modelled objective at each candidate rate (best deposit rate for each), with the "
        "reference rates marked.",
    )
    st.altair_chart(objective_curve(view.curve, _markers(view)), width="stretch")
    differences = scenario_differences(view)
    if differences:
        with st.expander("Scenario-to-scenario differences", expanded=False):
            st.dataframe(pd.DataFrame(differences), hide_index=True, width="stretch")

    ui.section("Decision-support summary")
    with st.container(border=True):
        st.markdown(build_decision_text(view))
    with st.expander("Model and scenario limitations", expanded=False):
        st.markdown(LIMITATIONS)


def _define(services: DashboardServices, overview):
    """Step 1. Returns the request, or None when no live term is available."""
    ui.section("1 · Define the scenario")
    with st.container(border=True):
        left = st.columns(3)
        term = live_term_selector(
            overview, "scenario_term", label="Mortgage term", container=left[0]
        )
        if term is None:
            return None
        strategy = left[1].selectbox(
            "Competitor benchmark",
            STRATEGIES,
            format_func=lambda value: STRATEGY_LABELS.get(value, value),
            key="scenario_strategy",
            help="How the competitor rate given to the optimiser is derived from the market.",
        )
        current = left[2].number_input(
            "Your current advertised rate (%) — optional",
            min_value=0.0,
            value=0.0,
            step=0.01,
            format="%.2f",
            key="scenario_current_rate",
            help="Your own advertised rate, used only to position you in the market. "
            "The optimiser does not read it. Leave at 0 if unknown.",
        )
        manual = offset = None
        second = st.columns(3)
        if strategy == "MANUAL":
            manual = second[0].number_input(
                "Manual competitor rate (%)",
                min_value=0.0,
                value=5.0,
                step=0.01,
                format="%.2f",
                key="scenario_manual_rate",
            )
        else:
            offset = second[0].number_input(
                "Offset to benchmark (bps)",
                value=0.0,
                step=1.0,
                key="scenario_offset",
                help="Shift the benchmark up (+) or down (−) in basis points.",
            )
        second[1].caption(
            f"Benchmark source: fresh comparable {fmt_term(term)} specials. "
            "Model-derived rates are labelled as such in the results."
        )
        with st.expander("What do these inputs mean?"):
            st.markdown(INPUT_GUIDE)
    return ScenarioRequest(
        term_months=term,
        strategy=strategy,
        manual_rate_pct=manual,
        offset_bps=offset or 0.0,
        current_rate_pct=current or None,
    )


def _scenarios_tab(services: DashboardServices) -> None:
    overview = unwrap(cached(services, "overview"))
    if overview is None:
        return
    request = _define(services, overview)
    if request is None:
        return

    run_col, note_col = st.columns([1, 4], vertical_alignment="center")
    if run_col.button(
        "Run scenario", type="primary", key="scenario_run", icon=":material/play_arrow:"
    ):
        with st.spinner("Running the optimiser…"):
            st.session_state[STATE_KEY] = (
                request,
                services.scenarios(request),
                services.db_stamp(),
                datetime.now(timezone.utc),
                services.config_fingerprint(),
            )

    stored = st.session_state.get(STATE_KEY)
    if stored is None:
        note_col.caption("Choose assumptions and click **Run scenario**.")
        return
    # Sessions created before configuration provenance was added must be rerun as well.
    stored_request, result, stored_stamp, computed_at = stored[:4]
    stored_config = stored[4] if len(stored) > 4 else None
    note_col.caption(f"Computed {fmt_nz_datetime(computed_at)} (Pacific/Auckland).")
    if stored_request != request:
        st.caption("Inputs changed since this result; click Run scenario to refresh.")
    if stored_stamp != services.db_stamp():
        st.caption(
            "Market data has changed since this result was computed; click Run scenario to refresh."
        )
    if stored_config != services.config_fingerprint():
        st.warning(
            "Engine configuration has changed since this result was computed. "
            "The previous recommendation is hidden; click Run scenario to refresh."
        )
        return
    if stored_request.term_months not in overview.selectable_terms:
        st.warning(
            "The result's market term is no longer available. "
            "The previous recommendation is hidden; click Run scenario to refresh."
        )
        return
    view = unwrap(result)
    if view is not None:
        _render_result(view)


def render(services: DashboardServices) -> None:
    ui.page_header(
        "Pricing Studio",
        "Test competitor assumptions against the pricing optimiser and see the trade-offs. "
        "Results are decision support for human review.",
    )
    st.warning(DISCLAIMER)
    scenarios_tab, lab_tab = st.tabs(["Guided scenario", "Advanced: Scenario Lab"])
    with scenarios_tab:
        _scenarios_tab(services)
    with lab_tab:
        scenario_lab.render(services)
