"""Operations: the only page that can start a collection.

Reading is separate from collecting. "Refresh Dashboard" only clears read caches. "Run Pricing
Watch" and "Dry Run" contact live bank websites, and start only from an explicit button click
(``on_click`` runs before the rerun, so the buttons are already locked when the page redraws).
A process-wide single-flight runner with a cooldown keeps a second click, a second browser tab,
or a burst of clicks from starting more than one collection.

Access: Streamlit has no built-in authentication. Set ``PRICING_WATCH_OPS_TOKEN`` to require a
shared secret before any operation is offered; otherwise the page says plainly that it is
unprotected. Every callback re-checks authorisation, so a locked session cannot act even if a
widget callback were invoked out of band.
"""

from __future__ import annotations

import hmac

import streamlit as st

from balance_pricing_engine.dashboard import actions
from balance_pricing_engine.dashboard.formatting import fmt_nz_datetime
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import OPS_TOKEN_ENV_VAR, DashboardSettings

NOTICE_KEY = "ops_notice"
QUICK_RESULT_KEY = "ops_quick_result"
CACHE_CLEARED_KEY = "ops_cache_cleared_for"
UNLOCKED_KEY = "ops_unlocked"
FAILED_ATTEMPTS_KEY = "ops_failed_attempts"
TOKEN_INPUT_KEY = "ops_token_input"
MAX_FAILED_ATTEMPTS = 5

STATUS_KIND = {
    "SUCCESS": st.success,
    "PARTIAL_SUCCESS": st.warning,
    "ALREADY_RUNNING": st.warning,
    "NOT_CONFIGURED": st.warning,
    "RATE_LIMITED": st.warning,
}


def _authorised(settings: DashboardSettings) -> bool:
    return settings.ops_token is None or st.session_state.get(UNLOCKED_KEY) is True


def _try_unlock(settings: DashboardSettings) -> None:
    """Button callback: compare the entered token in constant time, with an attempt cap."""
    if settings.ops_token is None:
        return
    if st.session_state.get(FAILED_ATTEMPTS_KEY, 0) >= MAX_FAILED_ATTEMPTS:
        st.session_state[NOTICE_KEY] = "Too many incorrect attempts; reload the page to try again."
        return
    supplied = str(st.session_state.get(TOKEN_INPUT_KEY, ""))
    st.session_state[TOKEN_INPUT_KEY] = ""  # never keep the entered secret around
    if hmac.compare_digest(supplied.encode(), settings.ops_token.encode()):
        st.session_state[UNLOCKED_KEY] = True
        st.session_state[FAILED_ATTEMPTS_KEY] = 0
    else:
        st.session_state[FAILED_ATTEMPTS_KEY] = st.session_state.get(FAILED_ATTEMPTS_KEY, 0) + 1
        st.session_state[NOTICE_KEY] = "Incorrect token."


def _start_job(name: str, action: str, settings: DashboardSettings) -> None:
    """Button callback: runs before the rerun, so the redraw sees the job as running."""
    if not _authorised(settings):
        return
    runner = actions.default_runner()
    # Resolved by name at call time (not bound at import) so the action is looked up late.
    refusal = runner.submit(name, lambda: getattr(actions, action)(settings))
    if refusal is not None:
        st.session_state[NOTICE_KEY] = refusal


def _run_quick(action: str, settings: DashboardSettings, *args) -> None:
    if not _authorised(settings):
        return
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


def _gate(settings: DashboardSettings) -> bool:
    """Render the access gate. True when operations may be offered to this session."""
    if settings.ops_token is None:
        st.info(
            "Operations are not access-protected: anyone who can open this dashboard can start "
            "collections. Run Streamlit with `--server.address localhost`, or set "
            f"`{OPS_TOKEN_ENV_VAR}` to require a token."
        )
        return True
    if _authorised(settings):
        return True
    st.subheader("Operations are locked")
    st.text_input("Operations token", type="password", key=TOKEN_INPUT_KEY)
    st.button("Unlock Operations", key="ops_unlock", on_click=_try_unlock, args=(settings,))
    notice = st.session_state.pop(NOTICE_KEY, None)
    if notice:
        st.error(notice)
    return False


def render(services: DashboardServices) -> None:
    settings = services.settings
    runner = actions.default_runner()

    st.subheader("Refresh Dashboard")
    st.caption("Reads the latest **stored** data only. It never contacts a bank.")
    if st.button("Refresh Dashboard", key="refresh_dashboard"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    if not _gate(settings):
        return

    state = runner.state()
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
        "Create Backup",
        key="create_backup",
        on_click=_run_quick,
        args=("create_backup", settings, settings),
    )
    right.button(
        "Send Test Notification",
        key="telegram_test",
        on_click=_run_quick,
        args=("send_test_notification", settings),
    )
    quick = st.session_state.get(QUICK_RESULT_KEY)
    if quick is not None:
        _show(quick)
