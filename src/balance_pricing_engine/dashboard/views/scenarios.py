"""Pricing Studio: what does the optimiser say under different competitor assumptions?

Decision support only. Three steps -- define, run & compare, understand the trade-offs -- with
the original YAML Scenario Lab one tab away for advanced use. The optimiser runs when the
operator clicks "Run scenario" -- never on a rerender -- and its result is kept in session state
with the inputs, the database stamp and the engine-config fingerprint it was computed from.
A changed config or an unavailable term hides the result until an explicit rerun.

Three scenario types, each backed by an existing backend seam (see
``DashboardServices.scenarios``):

- **Home loan** moves the competitor *loan* benchmark (the original guided scenario).
- **Term deposit** moves the competitor *deposit* benchmark, reading the term-deposit market.
- **Joint balance sheet** moves both at once and shows the optimiser's joint answer.

The optimiser always solves the loan and deposit rate together; the scenario type decides which
competitor input moves and which output is headlined. Each type keeps its own widgets and its
own stored result, so a mortgage result is never shown under a deposit heading.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard import ui
from balance_pricing_engine.dashboard.charts import (
    objective_curve,
    scenario_objectives,
    scenario_rates,
)
from balance_pricing_engine.dashboard.formatting import (
    fmt_bps,
    fmt_money_m,
    fmt_nz_datetime,
    fmt_rate_fraction,
    fmt_term,
)
from balance_pricing_engine.dashboard.models import (
    SCENARIO_HOME_LOAN,
    SCENARIO_JOINT,
    SCENARIO_TERM_DEPOSIT,
    ScenarioRequest,
    ScenarioView,
)
from balance_pricing_engine.dashboard.products import profile
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
#: The joint scenario overlays a market benchmark on both sides; there is no manual input.
JOINT_STRATEGIES = STRATEGIES[:-1]
STRATEGY_LABELS = {
    "MARKET_MEDIAN": "Market median",
    "MARKET_LEADER": "Market leader (lowest rate)",
    "MARKET_SECOND_LOWEST": "Second-lowest rate",
    "MARKET_MEAN": "Market mean",
    "MANUAL": "Manual competitor rate",
}
#: The backend's leader and second-best follow the product direction: highest for deposits.
DEPOSIT_STRATEGY_LABELS = {
    **STRATEGY_LABELS,
    "MARKET_LEADER": "Market leader (highest rate)",
    "MARKET_SECOND_LOWEST": "Second-highest rate",
}
STATE_KEY = "scenario_result"  # home-loan results; other modes append their mode

MODES = {
    SCENARIO_HOME_LOAN: ":material/home: Home loan scenario",
    SCENARIO_TERM_DEPOSIT: ":material/savings: Term deposit scenario",
    SCENARIO_JOINT: ":material/balance: Joint balance sheet",
}
MODE_NOTES = {
    SCENARIO_HOME_LOAN: (
        "⌂ Lending: moves the competitor home-loan benchmark and headlines the optimiser's "
        "home-loan rate, lending volume and risk-adjusted value."
    ),
    SCENARIO_TERM_DEPOSIT: (
        "◎ Deposits: moves the competitor term-deposit benchmark and headlines the optimiser's "
        "deposit rate, deposit uptake and funding cost. A higher deposit rate attracts deposits "
        "but raises the bank's funding cost."
    ),
    SCENARIO_JOINT: (
        "⇄ Both sides: moves both competitor benchmarks at once and shows the optimiser's joint "
        "loan and deposit answer, with the funding trade-off between them."
    ),
}

INPUT_GUIDE = """
- **Term** — which comparable advertised rate the competitor benchmark is read from: fixed
  mortgage specials for home loans, standard term deposits for deposits. Only terms with fresh
  market data are offered.
- **Competitor benchmark** — how the competitor rate fed into the optimiser is derived from the
  live market: its median, its leader, the second-best rate or the mean; or a rate you type in.
  For home loans the leader is the *lowest* rate; for term deposits it is the *highest*.
- **Offset to benchmark** — shifts the derived competitor rate, in basis points (+10 = 0.10%
  higher), to test "what if competitors move".
- **Your current advertised rate** — optional. It only positions you against the market (rank and
  gaps). The optimiser never reads it, and it is never confused with the model's output.
"""

LIMITATIONS = """
- The response model is calibrated on **synthetic** customers; the objective is a modelled,
  risk-adjusted value, not a forecast of profit.
- Pricing is **portfolio-level** (one home-loan rate and one term-deposit rate), not personalised.
- The optimiser always solves both rates together. A single-side scenario changes only that
  side's competitor assumption; the other side's assumption stays as configured.
- **No-change optimum** is the optimiser's answer with the competitor assumption unchanged — it is
  not your bank's advertised rate.
- **Model optimum** is the same optimiser run as **Match median** (the model's reference
  benchmark), shown for orientation; it is not a separately calculated strategy.
- A higher deposit rate attracts depositors but increases funding cost; the deposit market leader
  is not the best rate for bank margin.
- Sensitivity analysis and the full profit surface are in **Advanced: Scenario Lab**.
- Every output requires human review. Nothing here changes a price.
"""


def _key(mode: str, name: str) -> str:
    """Widget keys: the home-loan scenario keeps its original keys (bookmarked sessions, tests)."""
    return f"scenario_{name}" if mode == SCENARIO_HOME_LOAN else f"scenario_{name}_{mode}"


def _state_key(mode: str) -> str:
    return STATE_KEY if mode == SCENARIO_HOME_LOAN else f"{STATE_KEY}_{mode}"


def _side_product(mode: str) -> ProductType:
    return ProductType.TERM_DEPOSIT if mode == SCENARIO_TERM_DEPOSIT else ProductType.HOME_LOAN


def _strategy_labels(product: ProductType) -> dict[str, str]:
    return DEPOSIT_STRATEGY_LABELS if product is ProductType.TERM_DEPOSIT else STRATEGY_LABELS


def _markers(view: ScenarioView) -> dict[str, float]:
    info = profile(_side_product(view.mode))
    by_label = {row.label: row for row in view.rows}
    markers: dict[str, float] = {}
    if view.request.current_rate_pct is not None:
        markers["Current (entered)"] = view.request.current_rate_pct
    if "Match median" in by_label:
        markers["Market median"] = by_label["Match median"].competitor_rate * 100
    if "Match leader" in by_label:
        markers[f"Market leader ({info.best_word})"] = (
            by_label["Match leader"].competitor_rate * 100
        )
    if view.model_optimum_rate is not None:
        markers["Model optimum"] = view.model_optimum_rate * 100
    return markers


def _exclusion_warnings(exclusions, product: ProductType) -> None:
    info = profile(product)
    for exclusion in exclusions:
        st.warning(
            f"{info.symbol} {exclusion.bank} excluded from the {info.noun} benchmark: latest "
            f"validated observation is {exclusion.age_hours:.0f} h old "
            f"(limit {exclusion.threshold_hours:.0f} h)."
        )


def _governance(view: ScenarioView) -> None:
    if view.mode == SCENARIO_JOINT:
        _exclusion_warnings(view.excluded_banks, ProductType.HOME_LOAN)
        _exclusion_warnings(view.deposit_excluded_banks, ProductType.TERM_DEPOSIT)
    else:
        _exclusion_warnings(view.excluded_banks, _side_product(view.mode))
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


# -------------------------------------------------------------------------- results


def _home_loan_metrics(view: ScenarioView) -> None:
    selected = view.selected
    cols = ui.metric_columns(5, key="scenario-home-loan")
    cols[0].metric(
        "Optimal home-loan rate",
        fmt_rate_fraction(selected.our_rate),
        border=True,
        help="The optimiser's best home-loan rate under the selected competitor assumption.",
    )
    cols[1].metric(
        "Competitor mortgage assumption",
        fmt_rate_fraction(selected.competitor_rate),
        border=True,
        help="The competitor home-loan rate the optimiser was given (benchmark plus offset).",
    )
    cols[2].metric(
        "Objective",
        fmt_money_m(selected.objective),
        border=True,
        help="Modelled expected risk-adjusted value after the funding-shortfall penalty.",
    )
    cols[3].metric("Expected loan volume", fmt_money_m(selected.expected_loan_volume), border=True)
    cols[4].metric("Funding shortfall", fmt_money_m(selected.funding_shortfall), border=True)


def _deposit_metrics(view: ScenarioView) -> None:
    selected = view.selected
    cols = ui.metric_columns(3, key="scenario-deposit-rates")
    cols[0].metric(
        "Optimal term-deposit rate",
        fmt_rate_fraction(selected.our_rate),
        border=True,
        help="The optimiser's best term-deposit rate under the selected deposit competitor "
        "assumption.",
    )
    cols[1].metric(
        "Competitor deposit assumption",
        fmt_rate_fraction(selected.competitor_rate),
        border=True,
        help="The competitor term-deposit rate the optimiser was given (benchmark plus offset).",
    )
    cols[2].metric(
        "Objective",
        fmt_money_m(selected.objective),
        border=True,
        help="Modelled expected risk-adjusted value after the funding-shortfall penalty.",
    )
    cols = ui.metric_columns(3, key="scenario-deposit-volumes")
    cols[0].metric(
        "Expected deposit volume", fmt_money_m(selected.expected_deposit_volume), border=True
    )
    cols[1].metric(
        "Deposit funding cost",
        fmt_money_m(selected.deposit_interest_cost),
        border=True,
        help="Modelled interest the bank pays on the expected deposits at the optimal rate.",
    )
    cols[2].metric("Funding shortfall", fmt_money_m(selected.funding_shortfall), border=True)
    st.caption(
        f"The optimiser re-solved the home-loan rate too ({fmt_rate_fraction(selected.loan_rate)},"
        " with the configured competitor loan rate unchanged); it is context, not this "
        "scenario's result."
    )


def _joint_metrics(view: ScenarioView) -> None:
    selected = view.selected
    cols = ui.metric_columns(3, key="scenario-joint-rates")
    cols[0].metric("⌂ Optimal home-loan rate", fmt_rate_fraction(selected.loan_rate), border=True)
    cols[1].metric(
        "◎ Optimal term-deposit rate", fmt_rate_fraction(selected.deposit_rate), border=True
    )
    cols[2].metric(
        "Objective",
        fmt_money_m(selected.objective),
        border=True,
        help="Modelled expected risk-adjusted value of the joint loan and deposit book after the "
        "funding-shortfall penalty.",
    )
    cols = ui.metric_columns(3, key="scenario-joint-volumes")
    cols[0].metric(
        "⌂ Expected loan volume", fmt_money_m(selected.expected_loan_volume), border=True
    )
    cols[1].metric(
        "◎ Expected deposit volume", fmt_money_m(selected.expected_deposit_volume), border=True
    )
    cols[2].metric("Funding shortfall", fmt_money_m(selected.funding_shortfall), border=True)
    st.caption(
        f"Competitor assumptions: home loan {fmt_rate_fraction(selected.loan_competitor_rate)} · "
        f"term deposit {fmt_rate_fraction(selected.deposit_competitor_rate)}."
    )


def _comparison(view: ScenarioView) -> None:
    info = profile(_side_product(view.mode))
    ui.section(
        f"{info.market} scenario comparison",
        f"Each scenario re-runs the optimiser under a different competitor {info.noun} assumption.",
    )
    rate_tab, value_tab = st.tabs(["Modelled rates", "Modelled financial value"])
    with rate_tab:
        ui.render_chart(scenario_rates(view.rows, DISPLAY_LABELS))
    with value_tab:
        ui.render_chart(scenario_objectives(view.rows, DISPLAY_LABELS))
        st.caption(
            "Zero-based value axis. Negative values are modelled losses, not realised profit."
        )
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
        f"Rates are {info.rate_noun}s. Our rate is the optimiser's recommendation under each "
        "competitor assumption; Objective is the modelled risk-adjusted value. Model optimum is "
        "the Match median row, not a separate strategy."
    )


def _trade_offs(view: ScenarioView) -> None:
    if view.mode == SCENARIO_JOINT:
        ui.section(
            "3 · Understand the trade-offs",
            "The modelled objective along each side: at each candidate rate, the best objective "
            "across every rate on the other side.",
        )
        left, right = st.columns(2, gap="large")
        ui.render_chart(objective_curve(view.curve, {}), container=left)
        ui.render_chart(
            objective_curve(view.deposit_curve, {}, "Our term-deposit rate (%)"), container=right
        )
        return
    is_deposit = view.mode == SCENARIO_TERM_DEPOSIT
    other = "loan" if is_deposit else "deposit"
    ui.section(
        "3 · Understand the trade-offs",
        f"The modelled objective at each candidate rate (best {other} rate for each), with the "
        "reference rates marked.",
    )
    ui.render_chart(
        objective_curve(
            view.deposit_curve if is_deposit else view.curve,
            _markers(view),
            "Our term-deposit rate (%)" if is_deposit else "Our home-loan rate (%)",
        )
    )
    differences = scenario_differences(view)
    if differences:
        with st.expander("Scenario-to-scenario differences", expanded=False):
            st.dataframe(pd.DataFrame(differences), hide_index=True, width="stretch")


def _render_result(view: ScenarioView) -> None:
    st.badge("Human review required", icon=":material/person_check:", color="orange")
    _governance(view)
    selected = view.selected
    ui.section(
        "2 · Results",
        f"Selected assumption: {selected.rationale}. Rates below are modelled outputs.",
    )
    if view.mode == SCENARIO_JOINT:
        _joint_metrics(view)
    elif view.mode == SCENARIO_TERM_DEPOSIT:
        _deposit_metrics(view)
    else:
        _home_loan_metrics(view)
    if view.position is not None:
        pos = view.position
        info = profile(_side_product(view.mode))
        st.caption(
            f"Your entered current {info.rate_noun} vs market: rank #{pos.rank} of "
            f"{pos.bank_count}, {fmt_bps(pos.bps_vs_best)} vs leader ({info.best_word} rate), "
            f"{fmt_bps(pos.bps_vs_median)} vs median."
        )
    if view.rows:
        _comparison(view)
    else:
        st.caption(
            "No scenario comparison set for the joint scenario: the comparison set varies one "
            "side's competitor assumption at a time. Run a single-side scenario to compare."
        )
    _trade_offs(view)

    ui.section("Decision-support summary")
    with st.container(border=True):
        st.markdown(build_decision_text(view))
    with st.expander("Model and scenario limitations", expanded=False):
        st.markdown(LIMITATIONS)


# --------------------------------------------------------------------------- inputs


def _benchmark_inputs(mode: str, product: ProductType, overview, columns, *, suffix: str = ""):
    """(term, strategy, offset, manual) for one side. ``suffix`` keys the joint deposit side."""
    info = profile(product)
    labels = _strategy_labels(product)
    term = live_term_selector(
        overview,
        _key(mode, f"term{suffix}"),
        label=f"{info.noun.capitalize()} term",
        container=columns[0],
    )
    if term is None:
        return None
    options = JOINT_STRATEGIES if mode == SCENARIO_JOINT else STRATEGIES
    strategy = columns[1].selectbox(
        f"Competitor {info.noun} benchmark",
        options,
        format_func=lambda value: labels.get(value, value),
        key=_key(mode, f"strategy{suffix}"),
        help=f"How the competitor {info.rate_noun} given to the optimiser is derived from the "
        f"market. {info.leader_label}.",
    )
    manual = offset = None
    if strategy == "MANUAL":
        manual = columns[2].number_input(
            f"Manual competitor {info.rate_noun} (%)",
            min_value=0.0,
            value=5.0 if product is ProductType.HOME_LOAN else 4.0,
            step=0.01,
            format="%.2f",
            key=_key(mode, "manual_rate"),
        )
    else:
        offset = columns[2].number_input(
            "Offset to benchmark (bps)",
            value=0.0,
            step=1.0,
            key=_key(mode, f"offset{suffix}"),
            help="Shift the benchmark up (+) or down (−) in basis points.",
        )
    return term, strategy, offset or 0.0, manual


def _define(services: DashboardServices, mode: str):
    """Step 1. Returns the request, or None when a required market has no live term."""
    ui.section("1 · Define the scenario")
    st.caption(MODE_NOTES[mode])
    with st.container(border=True):
        if mode == SCENARIO_JOINT:
            request = _define_joint(services)
        else:
            request = _define_single(services, mode)
        with st.expander("What do these inputs mean?"):
            st.markdown(INPUT_GUIDE)
    return request


def _define_single(services: DashboardServices, mode: str):
    product = _side_product(mode)
    info = profile(product)
    overview = unwrap(cached(services, "overview", product))
    if overview is None:
        return None
    first, second = st.columns(3), st.columns(3)
    side = _benchmark_inputs(mode, product, overview, [first[0], first[1], second[0]])
    if side is None:
        return None
    term, strategy, offset, manual = side
    current = first[2].number_input(
        f"Your current advertised {info.rate_noun} (%) — optional",
        min_value=0.0,
        value=0.0,
        step=0.01,
        format="%.2f",
        key=_key(mode, "current_rate"),
        help="Your own advertised rate, used only to position you in the market. "
        "The optimiser does not read it. Leave at 0 if unknown.",
    )
    second[1].caption(
        f"Benchmark source: fresh comparable {fmt_term(term)} {info.tier}. "
        "Model-derived rates are labelled as such in the results."
    )
    return ScenarioRequest(
        term_months=term,
        strategy=strategy,
        manual_rate_pct=manual,
        offset_bps=offset,
        current_rate_pct=current or None,
        mode=mode,
    )


def _define_joint(services: DashboardServices):
    loan_overview = unwrap(cached(services, "overview", ProductType.HOME_LOAN))
    deposit_overview = unwrap(cached(services, "overview", ProductType.TERM_DEPOSIT))
    if loan_overview is None or deposit_overview is None:
        st.caption("The joint scenario needs fresh market data for both products.")
        return None
    lending, deposits = st.columns(2, gap="large")
    with lending:
        st.markdown("**⌂ Lending side**")
        loan = _benchmark_inputs(SCENARIO_JOINT, ProductType.HOME_LOAN, loan_overview, [st, st, st])
    with deposits:
        st.markdown("**◎ Deposit side**")
        deposit = _benchmark_inputs(
            SCENARIO_JOINT,
            ProductType.TERM_DEPOSIT,
            deposit_overview,
            [st, st, st],
            suffix="_deposit",
        )
    if loan is None or deposit is None:
        return None
    return ScenarioRequest(
        term_months=loan[0],
        strategy=loan[1],
        manual_rate_pct=None,
        offset_bps=loan[2],
        current_rate_pct=None,
        mode=SCENARIO_JOINT,
        deposit_term_months=deposit[0],
        deposit_strategy=deposit[1],
        deposit_offset_bps=deposit[2],
    )


def _still_available(services: DashboardServices, request: ScenarioRequest) -> bool:
    """Whether every market term the result was read from is still live."""
    needs = []
    if request.mode in (SCENARIO_HOME_LOAN, SCENARIO_JOINT):
        needs.append((ProductType.HOME_LOAN, request.term_months))
    if request.mode == SCENARIO_TERM_DEPOSIT:
        needs.append((ProductType.TERM_DEPOSIT, request.term_months))
    if request.mode == SCENARIO_JOINT:
        needs.append((ProductType.TERM_DEPOSIT, request.deposit_term_months))
    for product, term in needs:
        overview = cached(services, "overview", product).value
        if overview is None or term not in overview.selectable_terms:
            return False
    return True


def _scenarios_tab(services: DashboardServices) -> None:
    seed = (
        SCENARIO_TERM_DEPOSIT
        if ui.current_product() is ProductType.TERM_DEPOSIT
        else SCENARIO_HOME_LOAN
    )
    if st.session_state.get("scenario_mode") not in MODES:
        st.session_state["scenario_mode"] = seed
    with st.container(key="pw-product-switch-scenario"):
        mode = st.segmented_control(
            "Scenario type",
            list(MODES),
            key="scenario_mode",
            required=True,
            format_func=MODES.get,
            help="Which competitor benchmark the scenario moves. The optimiser always solves "
            "the home-loan and term-deposit rates together.",
        )
    mode = mode or SCENARIO_HOME_LOAN
    if mode != SCENARIO_JOINT:
        ui.set_product(_side_product(mode))
        ui.product_banner(_side_product(mode))

    request = _define(services, mode)
    if request is None:
        return

    state_key = _state_key(mode)
    run_col, note_col = st.columns([1, 4], vertical_alignment="center")
    if run_col.button(
        "Run scenario", type="primary", key=_key(mode, "run"), icon=":material/play_arrow:"
    ):
        with st.spinner("Running the optimiser…"):
            st.session_state[state_key] = (
                request,
                services.scenarios(request),
                services.db_stamp(),
                datetime.now(timezone.utc),
                services.config_fingerprint(),
            )

    stored = st.session_state.get(state_key)
    if stored is None:
        note_col.caption("Choose assumptions and click **Run scenario**.")
        return
    # Sessions created before configuration provenance was added must be rerun as well.
    stored_request, result, stored_stamp, computed_at = stored[:4]
    stored_config = stored[4] if len(stored) > 4 else None
    note_col.caption(f"Computed {fmt_nz_datetime(computed_at)} (Pacific/Auckland).")
    if stored_request.mode != mode:  # defensive: a result never renders under another type
        return
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
    if not _still_available(services, stored_request):
        st.warning(
            "The result's market term is no longer available. "
            "The previous recommendation is hidden; click Run scenario to refresh."
        )
        return
    view = unwrap(result)
    if view is not None and view.mode == mode:
        _render_result(view)


def render(services: DashboardServices) -> None:
    ui.page_header(
        "Pricing Studio",
        "Test competitor assumptions for home loans, term deposits or both against the pricing "
        "optimiser and see the trade-offs. Results are decision support for human review.",
    )
    st.warning(DISCLAIMER)
    ui.workflow_steps()
    scenarios_tab, lab_tab = st.tabs(["Guided scenario", "Advanced: Scenario Lab"])
    with scenarios_tab:
        _scenarios_tab(services)
    with lab_tab:
        scenario_lab.render(services)
