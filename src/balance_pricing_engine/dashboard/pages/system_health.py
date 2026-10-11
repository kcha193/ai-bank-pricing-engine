"""System Health: is Pricing Watch itself healthy and current?

Everything here is the backend's own model (``pricing_intelligence.health``) evaluated now and
never cached. Text that comes from banks or from error messages (``last_error``, run errors) is
shown only in dataframe cells, which render literally, never through Markdown.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from balance_pricing_engine.competitor_rates.models import ProductType
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
from balance_pricing_engine.dashboard.pages._common import cached, unwrap
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.pricing_intelligence.health import (
    CollectorStatus,
    FreshnessState,
    HealthLevel,
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
                "Health": fmt_health(b.health),
                "Current records": b.current_record_count,
                "Last error": b.last_error,
            }
            for b in health.banks
        ]
    )


def _explain(health: SystemHealth, exclusions) -> None:
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


def render(services: DashboardServices) -> None:
    health = unwrap(services.health())
    if health is None:
        return
    st.subheader("Components")
    st.dataframe(_components(health), hide_index=True, width="stretch")
    st.caption(
        "Pricing Watch does not observe the external scheduler; the last run's age is the only signal."
    )
    tabs = st.tabs([label for label, _ in PRODUCT_TABS])
    for tab, (_, product) in zip(tabs, PRODUCT_TABS):
        with tab:
            _product_tab(services, product)
