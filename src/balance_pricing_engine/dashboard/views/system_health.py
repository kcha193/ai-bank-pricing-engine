"""System Health: is Pricing Watch itself healthy and current?

Everything here is the backend's own model (``pricing_intelligence.health``) evaluated now and
never cached. Text that comes from banks or from error messages (``last_error``, run errors) is
shown only in dataframe cells, which render literally, never through Markdown.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard import ui
from balance_pricing_engine.dashboard.charts import freshness_bars
from balance_pricing_engine.dashboard.products import profile
from balance_pricing_engine.dashboard.formatting import (
    EM_DASH,
    fmt_age_hours,
    fmt_bps,
    fmt_freshness,
    fmt_health,
    fmt_nz_datetime,
    fmt_rate_pct,
    fmt_term,
)
from balance_pricing_engine.dashboard.views._common import cached, unwrap
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.pricing_intelligence.health import (
    CollectorStatus,
    FreshnessState,
    HealthLevel,
    RunHealth,
    SystemHealth,
)

PRODUCT_TABS = (("Mortgage", ProductType.HOME_LOAN), ("Term deposit", ProductType.TERM_DEPOSIT))


def _yes_no(value: bool) -> str:
    return "CONFIGURED" if value else "NOT CONFIGURED"


def _components(health: SystemHealth) -> pd.DataFrame:
    run = health.latest_run
    last_run_detail = EM_DASH
    if run is not None:
        age_hours = (health.evaluated_at - run.started_at).total_seconds() / 3600
        last_run_detail = f"{fmt_nz_datetime(run.started_at)} · {fmt_age_hours(age_hours)} ago"
    rows = [
        (
            "Database",
            "HEALTHY" if health.database_ok else "ISSUES DETECTED",
            "; ".join(health.database_issues) or "Integrity checks passed",
        ),
        (
            "Market snapshot",
            "CURRENT" if health.snapshot_valid else "EMPTY",
            f"Observations older than {health.max_observation_age_hours:.0f} h are excluded",
        ),
        ("Last run", run.status if run else "NONE RECORDED", last_run_detail),
        ("Latest collection", health.run_health.value, health.collection_summary),
        ("Telegram", _yes_no(health.telegram_configured), ""),
        ("Webhook", _yes_no(health.webhook_configured), ""),
    ]
    return pd.DataFrame(rows, columns=["Component", "Status", "Detail"])


def _collectors(health: SystemHealth) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Bank": b.bank,
                "Last success": fmt_nz_datetime(b.last_success_at),
                "Observation age": fmt_age_hours(b.freshness.age_hours),
                "Failures": b.consecutive_failures,
                "Freshness": fmt_freshness(b.freshness.state),
                "Operational status": b.operational.label if b.operational else EM_DASH,
                "Latest attempt": (
                    f"{fmt_nz_datetime(b.operational.latest_attempt_at)} · "
                    f"{b.operational.collection.value}"
                    if b.operational and b.operational.latest_attempt_at
                    else EM_DASH
                ),
                "Health": fmt_health(b.health),
                "Current records": b.current_record_count,
                "Last error": b.last_error,
            }
            for b in health.banks
        ]
    )


def _explain(health: SystemHealth, exclusions) -> None:
    if health.run_health in (RunHealth.FAILED, RunHealth.DEGRADED):
        # A failed or partial attempt must never read as a refresh (DATA-004).
        st.warning(
            f"{health.collection_summary}. Stored observations were not refreshed by that "
            "attempt; their ages below are unchanged."
        )
    for bank in health.banks:
        if bank.collector is CollectorStatus.FAILING:
            observation_message = {
                FreshnessState.FRESH: "Its last validated observation is still current.",
                FreshnessState.STALE: (
                    "Its last validated observation remains available but is marked STALE."
                ),
                FreshnessState.UNKNOWN: (
                    "No confirmed validated observation is available; freshness is UNKNOWN."
                ),
            }[bank.freshness.state]
            st.warning(
                f"{bank.bank} collection failed. "
                + observation_message
                + " See the collector table below."
            )
        elif bank.health is HealthLevel.DEGRADED:
            st.warning(f"{bank.bank} needs review: observations are stale or quarantined.")
    for exclusion in exclusions:
        st.warning(
            f"{exclusion.bank} excluded from market statistics at {fmt_term(exclusion.term_months)}: "
            f"latest validated observation is {exclusion.age_hours:.0f} h old "
            f"(limit {exclusion.threshold_hours:.0f} h)."
        )


def _runs(services: DashboardServices, product: ProductType) -> None:
    st.subheader("Recent runs")
    runs = unwrap(cached(services, "runs", product))
    if runs is None:
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Run id": r.run_id,
                    "Time": fmt_nz_datetime(r.started_at),
                    "Status": r.status,
                    "Banks OK": r.banks_ok,
                    "Banks failed": r.banks_failed,
                    "Observations": r.observations,
                    "Changes": r.changes,
                    "Duration": f"{r.duration_seconds:.0f} s",
                }
                for r in runs
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    labels = [f"{n}. {fmt_nz_datetime(r.started_at)} · {r.status}" for n, r in enumerate(runs, 1)]
    chosen = st.selectbox("Run detail", labels, key=f"run_detail_{product.value}")
    detail = unwrap(cached(services, "run_detail", runs[labels.index(chosen)].run_id))
    if detail is None:
        return
    st.dataframe(
        pd.DataFrame(
            [
                ("Banks successful", ", ".join(detail.banks_successful) or EM_DASH),
                ("Banks failed", ", ".join(detail.banks_failed) or EM_DASH),
                ("Validation failures", str(detail.validation_failures)),
                ("Suspicious observations", str(detail.suspicious)),
                ("Market state", detail.market_state or EM_DASH),
                ("Error summary", detail.error_summary or EM_DASH),
            ],
            columns=["Field", "Value"],
        ),
        hide_index=True,
        width="stretch",
    )


def _product_tab(services: DashboardServices, product: ProductType) -> None:
    result = services.health(product)  # evaluated now, never cached
    health = unwrap(result)
    if health is None:
        return
    info = profile(product)
    ui.product_banner(product)
    _overview_tiles(services, health, product)
    st.subheader(f"{info.market} data freshness by bank", anchor=False)
    st.caption(
        f"Age of each bank's latest validated {info.noun} observation. Bars past the dashed "
        "limit are stale and excluded from market statistics."
    )
    with ui.chart_panel(f"freshness-{product.value}"):
        ui.render_chart(freshness_bars(health))
    with st.expander(f"{info.market} system components"):
        st.dataframe(_components(health), hide_index=True, width="stretch")
    exclusions = unwrap(services.exclusions(product)) or ()
    _explain(health, exclusions)
    st.subheader("Collector health")
    st.dataframe(_collectors(health), hide_index=True, width="stretch")
    _runs(services, product)
    _suspicious(services, product)


def _suspicious(services: DashboardServices, product: ProductType) -> None:
    result = services.suspicious(product)
    if result.status != "OK" or not result.value:
        return
    st.subheader("Suspicious observations — Excluded from trusted benchmark")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Bank": r.bank,
                    "Term": fmt_term(r.term_months),
                    "Previous": fmt_rate_pct(r.previous_pct),
                    "Observed": fmt_rate_pct(r.observed_pct),
                    "Movement": fmt_bps(r.change_bps),
                    "Detected": fmt_nz_datetime(r.detected_at),
                    "Status": "SUSPICIOUS — excluded from trusted benchmark",
                }
                for r in result.value
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption("These readings are quarantined. They never feed rankings, medians or scenarios.")


DATA_STATES = """
| You see | It means |
| --- | --- |
| **No rate changes** | Every bank that was collected kept its rates. A quiet market, not missing data. |
| **No market data** | Nothing has been collected yet (or the database is unavailable). |
| **⚠ Stale** | The latest validated observation is older than the freshness limit; it is excluded from rankings and statistics. |
| **Collection failing** | The collector could not read the bank's page. Its last validated rate is kept and ages normally; it is never treated as a withdrawal. |
| **Degraded** | The rate is still inside the age limit, but the latest collection attempt failed or skipped the bank. The rate was not refreshed. |
| **Unknown** | No collection run is recorded for the product, so a stored rate is not treated as confirmed. |
| **Insufficient coverage** | Fewer distinct eligible banks than the configured minimum: rates are shown as observed data, and market-derived scenarios are refused. |
| **Quarantined** | A reading failed plausibility checks. The last confirmed rate is shown and labelled instead. |
| **Incomplete coverage** | Fewer banks are fresh than are configured, so market statistics cover fewer banks. |
"""


RUN_STATUS_SHORT = {"SUCCESS": "Success", "PARTIAL_SUCCESS": "Partial", "FAILED": "Failed"}


def _overview_tiles(
    services: DashboardServices, health: SystemHealth, product: ProductType
) -> None:
    info = profile(product)
    prefix = "Term deposit · " if product is ProductType.TERM_DEPOSIT else ""
    run = health.latest_run
    failing = sum(1 for b in health.banks if b.collector is CollectorStatus.FAILING)
    suspicious = services.suspicious(product)
    quarantined = len(suspicious.value) if suspicious.status == "OK" and suspicious.value else 0
    cols = ui.metric_columns(6, key=f"health-{product.value}")
    cols[0].metric(
        prefix + "Database", "HEALTHY" if health.database_ok else "⚠ ISSUES", border=True
    )
    run_age = EM_DASH
    if run is not None:
        run_age = (
            f"{fmt_age_hours((health.evaluated_at - run.started_at).total_seconds() / 3600)} ago"
        )
    cols[1].metric(
        prefix + "Latest run",
        RUN_STATUS_SHORT.get(run.status, run.status) if run else "None",
        border=True,
        help=f"Status {run.status}" if run else "No operational run recorded.",
    )
    cols[2].metric(prefix + "Last run age", run_age, border=True)
    cols[3].metric(
        prefix + "Fresh banks",
        f"{health.confirmed_fresh_bank_count} / {len(health.banks)}",
        border=True,
        help=f"Banks with observations newer than {health.max_observation_age_hours:.0f} h "
        f"that were collected successfully in the latest attempt. "
        f"{health.fresh_bank_count} have observations inside the age limit.",
    )
    cols[4].metric(prefix + "Collectors failing", failing, border=True)
    cols[5].metric(
        prefix + "Suspicious readings",
        quarantined,
        border=True,
        help=f"Implausible {info.noun} readings recorded so far (all time). They are quarantined: "
        "never part of a ranking, median or scenario. Listed below.",
    )
    channels = [
        f"Telegram {'✓ configured' if health.telegram_configured else '✗ not configured'}",
        f"Webhook {'✓ configured' if health.webhook_configured else '✗ not configured'}",
    ]
    st.caption("Alert channels: " + " · ".join(channels))


def render(services: DashboardServices) -> None:
    with st.expander("How to read data states"):
        st.markdown(DATA_STATES)
    st.caption(
        "Pricing Watch does not observe the external scheduler; the last run's age is the only signal."
    )
    tabs = st.tabs([label for label, _ in PRODUCT_TABS])
    for tab, (_, product) in zip(tabs, PRODUCT_TABS):
        with tab:
            _product_tab(services, product)
