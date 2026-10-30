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
from pathlib import Path

import streamlit as st

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard import actions, guard
from balance_pricing_engine.dashboard.formatting import fmt_nz_datetime
from balance_pricing_engine.dashboard.products import PRODUCTS
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import (
    MIN_OPS_TOKEN_LENGTH,
    OPS_TOKEN_ENV_VAR,
    DashboardSettings,
)

NOTICE_KEY = "ops_notice"
QUICK_RESULT_KEY = "ops_quick_result"
CACHE_CLEARED_KEY = "ops_cache_cleared_for"
UNLOCKED_KEY = "ops_unlocked"
TOKEN_INPUT_KEY = "ops_token_input"
MARKETS_KEY = "ops_markets"

#: Which markets a run covers. Both by default: a collection that silently skips term deposits
#: leaves the deposit market to go stale while the page reports success.
MARKET_CHOICES: dict[str, tuple[ProductType, ...]] = {
    "Both markets": actions.ALL_PRODUCTS,
    PRODUCTS[ProductType.HOME_LOAN].label: (ProductType.HOME_LOAN,),
    PRODUCTS[ProductType.TERM_DEPOSIT].label: (ProductType.TERM_DEPOSIT,),
}

STATUS_KIND = {
    "SUCCESS": st.success,
    "PARTIAL_SUCCESS": st.warning,
    "ALREADY_RUNNING": st.warning,
    "NOT_CONFIGURED": st.warning,
    "RATE_LIMITED": st.warning,
}


def _token_too_short(settings: DashboardSettings) -> bool:
    return settings.ops_token is not None and len(settings.ops_token) < MIN_OPS_TOKEN_LENGTH


def _authorised(settings: DashboardSettings) -> bool:
    if _token_too_short(settings):
        return False  # fail closed: a guessable token is no gate at all
    return settings.ops_token is None or st.session_state.get(UNLOCKED_KEY) is True


def _try_unlock(settings: DashboardSettings) -> None:
    """Button callback: charge the process-wide limiter first, then compare in constant time."""
    if settings.ops_token is None or _token_too_short(settings):
        return
    supplied = str(st.session_state.get(TOKEN_INPUT_KEY, ""))
    st.session_state[TOKEN_INPUT_KEY] = ""  # never keep the entered secret around
    limiter = guard.unlock_attempts()
    wait = limiter.try_acquire()  # atomic: charged before comparing, so bursts cannot race it
    if wait > 0:
        st.session_state[NOTICE_KEY] = (
            f"Too many incorrect attempts; unlocking is disabled for {wait:.0f} more seconds."
        )
        return
    if hmac.compare_digest(supplied.encode(), settings.ops_token.encode()):
        limiter.record_success()
        st.session_state[UNLOCKED_KEY] = True
    else:
        st.session_state[NOTICE_KEY] = "Incorrect token."


def _start_job(name: str, action: str, settings: DashboardSettings) -> None:
    """Button callback: runs before the rerun, so the redraw sees the job as running."""
    if not _authorised(settings):
        return
    runner = actions.default_runner()
    # Read now, at click time, so the job covers what was selected when the button was pressed.
    products = MARKET_CHOICES.get(st.session_state.get(MARKETS_KEY), actions.ALL_PRODUCTS)
    # Resolved by name at call time (not bound at import) so the action is looked up late.
    refusal = runner.submit(name, lambda: getattr(actions, action)(settings, products=products))
    if refusal is not None:
        st.session_state[NOTICE_KEY] = refusal


def _run_quick(action: str, settings: DashboardSettings, *args) -> None:
    if not _authorised(settings):
        return
    st.session_state[QUICK_RESULT_KEY] = getattr(actions, action)(*args)


def _display_path(path: str, repo_root: Path) -> str:
    """Repo-relative when inside the checkout, else just the file name: never reveal the
    server's absolute filesystem layout to whoever is looking at the page."""
    try:
        return Path(path).resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return Path(path).name


def _show(result: actions.ActionResult, settings: DashboardSettings) -> None:
    STATUS_KIND.get(result.status, st.error)(f"{result.status}: {result.message}")
    st.caption(f"Completed {fmt_nz_datetime(result.completed_at)} · {result.elapsed_seconds:.0f} s")
    for part in result.parts:  # one section per market, each with its own bank results
        st.markdown(f"**{PRODUCTS[part.product_type].tag}**")
        _show(part, settings)
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
        st.write(f"Backup written to `{_display_path(str(summary['path']), settings.repo_root)}`")


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
    if _token_too_short(settings):
        st.error(
            f"`{OPS_TOKEN_ENV_VAR}` must be at least {MIN_OPS_TOKEN_LENGTH} characters. "
            "Operations stay disabled until it is fixed."
        )
        return False
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
        "alerts if a channel is configured. Each market is collected and reported separately."
    )
    st.radio(
        "Markets",
        list(MARKET_CHOICES),
        key=MARKETS_KEY,
        horizontal=True,
        disabled=state.running,
        help="Applies to Run Pricing Watch and Dry Run.",
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
        _show(state.result, settings)
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
        _show(quick, settings)
