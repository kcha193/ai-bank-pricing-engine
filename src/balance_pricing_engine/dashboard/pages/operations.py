"""Operations: the only page that can start a collection.

Reading is separate from collecting. "Refresh Dashboard" only clears read caches. "Run Pricing
Watch" and "Dry Run" contact live bank websites, and start only from an explicit button click
(``on_click`` runs before the rerun, so the buttons are already locked when the page redraws).
A process-wide single-flight runner keeps a second click or a second browser tab from starting
a second collection.
"""

from __future__ import annotations

import streamlit as st

from balance_pricing_engine.dashboard import actions
from balance_pricing_engine.dashboard.formatting import fmt_nz_datetime
from balance_pricing_engine.dashboard.services import DashboardServices

NOTICE_KEY = "ops_notice"
QUICK_RESULT_KEY = "ops_quick_result"
CACHE_CLEARED_KEY = "ops_cache_cleared_for"
STATUS_KIND = {
    "SUCCESS": st.success,
    "PARTIAL_SUCCESS": st.warning,
    "ALREADY_RUNNING": st.warning,
    "NOT_CONFIGURED": st.warning,
}


def _start_job(name: str, action: str, settings) -> None:
    """Button callback: runs before the rerun, so the redraw sees the job as running."""
    runner = actions.default_runner()
    # Resolved by name at call time (not bound at import) so the action is looked up late.
    started = runner.submit(name, lambda: getattr(actions, action)(settings))
    if not started:
        st.session_state[NOTICE_KEY] = "A run is already in progress; nothing was started."


def _run_quick(action: str, *args) -> None:
    st.session_state[QUICK_RESULT_KEY] = getattr(actions, action)(*args)


def _show(result: actions.ActionResult) -> None:
    STATUS_KIND.get(result.status, st.error)(f"{result.status}: {result.message}")
    st.caption(f"Completed {fmt_nz_datetime(result.completed_at)} · {result.elapsed_seconds:.0f} s")
    if result.banks:
        st.dataframe(
            [
                {"Bank": b.bank, "Result": "SUCCESS" if b.ok else "FAILED", "Detail": b.detail}
                for b in result.banks
            ],
            hide_index=True,
            width="stretch",
        )
    summary = result.summary
    if "changes_detected" in summary:
        st.write(
            f"Rates accepted: {summary.get('rates_accepted', '—')} · "
            f"Changes detected: {summary.get('changes_detected', '—')} · "
            f"Suspicious: {summary.get('suspicious_changes', '—')}"
        )
    if "path" in summary:
        st.write(f"Backup written to `{summary['path']}`")


@st.fragment(run_every="3s")
def _watch_for_completion(runner: actions.JobRunner) -> None:
    """Mounted only while a job runs: one full rerun when it ends unlocks the buttons."""
    if not runner.state().running:
        st.rerun()


def render(services: DashboardServices) -> None:
    settings = services.settings
    runner = actions.default_runner()
    state = runner.state()

    st.subheader("Refresh Dashboard")
    st.caption("Reads the latest **stored** data only. It never contacts a bank.")
    if st.button("Refresh Dashboard", key="refresh_dashboard"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("Run Pricing Watch")
    st.caption(
        "Collects **live** rates from every tracked bank website, validates and stores them, "
        "detects changes and updates the market snapshot. It takes several minutes, and sends "
        "alerts if a channel is configured."
    )
    confirmed = st.checkbox("I understand this contacts live bank websites", key="confirm_live")
    st.button(
        "Run Pricing Watch",
        key="run_live",
        type="primary",
        disabled=not confirmed or state.running,
        on_click=_start_job,
        args=("live", "run_live", settings),
    )
    st.button(
        "Dry Run",
        key="run_dry",
        disabled=state.running,
        on_click=_start_job,
        args=("dry_run", "run_dry", settings),
    )
    st.caption(
        "Dry Run collects live pages and classifies them as normal, but persists nothing, "
        "sends no alerts and leaves collector health untouched."
    )

    notice = st.session_state.pop(NOTICE_KEY, None)
    if notice:
        st.warning(notice)
    if state.running:
        st.info(f"Running: {state.name}… this page updates when it finishes.")
        _watch_for_completion(runner)
    elif state.result is not None:
        _show(state.result)
        if state.name == "live" and st.session_state.get(CACHE_CLEARED_KEY) is not state.result:
            st.cache_data.clear()  # new data is stored; reads must not serve the old cache
            st.session_state[CACHE_CLEARED_KEY] = state.result

    st.divider()
    st.subheader("Maintenance")
    left, right = st.columns(2)
    left.button(
        "Create Backup", key="create_backup", on_click=_run_quick, args=("create_backup", settings)
    )
    right.button(
        "Send Test Notification",
        key="telegram_test",
        on_click=_run_quick,
        args=("send_test_notification",),
    )
    quick = st.session_state.get(QUICK_RESULT_KEY)
    if quick is not None:
        _show(quick)
