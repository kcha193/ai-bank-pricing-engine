"""Safety tests for the Operations page: only an explicit click may start a collection."""

import threading
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from dashboard_harness import run_page
from streamlit.proto.TextInput_pb2 import TextInput as TextInputProto

from balance_pricing_engine.dashboard import actions, guard

DONE = datetime(2026, 10, 11, tzinfo=timezone.utc)


def ok(name, status="SUCCESS", message="done"):
    return actions.ActionResult(name, status, message, DONE)


@pytest.fixture(autouse=True)
def fresh_unlock_limiter(monkeypatch):
    """The unlock limiter is process-wide; give every test its own."""
    limiter = guard.AttemptLimiter()
    monkeypatch.setattr(guard, "_UNLOCK_ATTEMPTS", limiter)
    return limiter


@pytest.fixture(autouse=True)
def clean_runner(monkeypatch):
    """A fresh process-wide runner per test, with no cooldown unless a test asks for one."""
    runner = actions.JobRunner(cooldown_seconds=0)
    monkeypatch.setattr(actions, "_DEFAULT_RUNNER", runner)
    yield runner
    runner.wait()


def test_rendering_and_refresh_never_collect(market_factory):
    market = market_factory()
    with patch.object(actions, "run_live") as live, patch.object(actions, "run_dry") as dry:
        app = run_page("operations", market.database, market.now)
        for _ in range(3):
            app.run()
        app.button(key="refresh_dashboard").click().run()
        assert not app.exception
    assert live.call_count == 0 and dry.call_count == 0


def test_run_requires_confirmation_then_launches_exactly_once(market_factory, clean_runner):
    market = market_factory()
    with patch.object(actions, "run_live", side_effect=lambda *a, **k: ok("live")) as live:
        app = run_page("operations", market.database, market.now)
        assert app.button(key="run_live").disabled  # not confirmed yet
        app.checkbox(key="confirm_live").check().run()
        assert not app.button(key="run_live").disabled
        app.button(key="run_live").click().run()
        clean_runner.wait()
        app.run()
        app.run()  # extra reruns must not relaunch
    assert live.call_count == 1
    assert any("SUCCESS" in s.value for s in app.success)


def test_dry_run_calls_dry_run_only(market_factory, clean_runner):
    market = market_factory()
    with (
        patch.object(actions, "run_dry", side_effect=lambda *a, **k: ok("dry_run")) as dry,
        patch.object(actions, "run_live") as live,
    ):
        app = run_page("operations", market.database, market.now)
        app.button(key="run_dry").click().run()
        clean_runner.wait()
    assert dry.call_count == 1 and live.call_count == 0


def test_a_second_click_while_running_does_not_launch_twice(market_factory, clean_runner):
    market = market_factory()
    gate = threading.Event()

    def slow(*args, **kwargs):
        gate.wait(5)
        return ok("live")

    with patch.object(actions, "run_live", side_effect=slow) as live:
        app = run_page("operations", market.database, market.now)
        app.checkbox(key="confirm_live").check().run()
        app.button(key="run_live").click().run()
        assert app.button(key="run_live").disabled  # buttons lock while a job runs
        assert any("Running" in i.value for i in app.info)
        app.button(key="run_dry").click().run()  # a different action is refused too
        gate.set()
        clean_runner.wait()
    assert live.call_count == 1


def test_a_run_started_in_another_tab_blocks_this_one(market_factory, clean_runner):
    market = market_factory()
    gate = threading.Event()
    clean_runner.submit("live", lambda: (gate.wait(5), ok("live"))[1])  # "other tab"
    with patch.object(actions, "run_live") as live:
        app = run_page("operations", market.database, market.now)
        assert app.button(key="run_live").disabled and app.button(key="run_dry").disabled
    gate.set()
    clean_runner.wait()
    assert live.call_count == 0


def test_already_running_exit_is_shown_as_a_warning(market_factory, clean_runner):
    market = market_factory()
    result = ok("live", "ALREADY_RUNNING", "Another Pricing Watch run is in progress")
    with patch.object(actions, "run_live", return_value=result):
        app = run_page("operations", market.database, market.now)
        app.checkbox(key="confirm_live").check().run()
        app.button(key="run_live").click().run()
        clean_runner.wait()
        app.run()
    assert any("ALREADY_RUNNING" in w.value for w in app.warning)


def test_bank_results_are_listed_after_a_partial_run(market_factory, clean_runner):
    market = market_factory()
    result = actions.ActionResult(
        "live",
        "PARTIAL_SUCCESS",
        "Run finished but some banks failed",
        DONE,
        banks=(actions.BankOutcome("ASB", True), actions.BankOutcome("Westpac", False, "Timeout")),
        summary={"rates_accepted": 14, "changes_detected": 2, "suspicious_changes": 0},
    )
    with patch.object(actions, "run_live", return_value=result):
        app = run_page("operations", market.database, market.now)
        app.checkbox(key="confirm_live").check().run()
        app.button(key="run_live").click().run()
        clean_runner.wait()
        app.run()
    table = app.dataframe[0].value
    assert dict(zip(table["Bank"], table["Result"])) == {"ASB": "SUCCESS", "Westpac": "FAILED"}
    assert any("Changes detected: 2" in m.value for m in app.markdown)


def test_telegram_not_configured_is_a_warning_and_shows_no_secret_names(
    market_factory, monkeypatch
):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    market = market_factory()
    app = run_page("operations", market.database, market.now)
    app.button(key="telegram_test").click().run()
    assert any("not configured" in w.value.lower() for w in app.warning)
    assert not any("TELEGRAM_BOT_TOKEN" in w.value for w in app.warning)


def test_backup_button_reports_the_written_location(market_factory, tmp_path):
    market = market_factory()
    backup = actions.ActionResult(
        "backup", "SUCCESS", "Backup created.", DONE, summary={"path": "/safe/backups/x.sqlite3"}
    )
    with patch.object(actions, "create_backup", return_value=backup):
        app = run_page("operations", market.database, market.now)
        app.button(key="create_backup").click().run()
    assert any("/safe/backups/x.sqlite3" in m.value for m in app.markdown)


def test_operations_page_works_without_a_database(tmp_path):
    app = run_page("operations", tmp_path / "none.db", datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not app.exception


def test_a_cooldown_between_runs_is_explained_not_silently_ignored(market_factory, monkeypatch):
    runner = actions.JobRunner(cooldown_seconds=60)
    monkeypatch.setattr(actions, "_DEFAULT_RUNNER", runner)
    market = market_factory()
    with patch.object(actions, "run_dry", side_effect=lambda *a, **k: ok("dry_run")) as dry:
        app = run_page("operations", market.database, market.now)
        app.button(key="run_dry").click().run()
        runner.wait()
        app.run()
        app.button(key="run_dry").click().run()
        runner.wait()
    assert dry.call_count == 1
    assert any("Please wait" in w.value and "nothing was started" in w.value for w in app.warning)


def test_rate_limited_actions_are_warnings(market_factory):
    market = market_factory()
    limited = ok("backup", "RATE_LIMITED", "A backup was created 3 minutes ago")
    with patch.object(actions, "create_backup", return_value=limited):
        app = run_page("operations", market.database, market.now)
        app.button(key="create_backup").click().run()
    assert any("RATE_LIMITED" in w.value for w in app.warning)


TOKEN_ENV = {"PRICING_WATCH_OPS_TOKEN": "s3cret-token"}


def test_with_a_token_configured_operations_are_locked_until_it_is_entered(market_factory):
    market = market_factory()
    app = run_page("operations", market.database, market.now, env=TOKEN_ENV)
    assert not app.exception
    for key in ("run_live", "run_dry", "create_backup", "telegram_test"):
        with pytest.raises(KeyError):
            app.button(key=key)
    assert app.button(key="refresh_dashboard") is not None  # reading stays available
    assert app.text_input(key="ops_token_input").proto.type == TextInputProto.PASSWORD
    assert all("s3cret-token" not in str(e.value) for e in (*app.markdown, *app.info, *app.error))


def test_the_right_token_unlocks_and_a_wrong_one_does_not(market_factory):
    market = market_factory()
    with patch.object(actions, "run_live") as live:
        app = run_page("operations", market.database, market.now, env=TOKEN_ENV)
        app.text_input(key="ops_token_input").set_value("wrong").run()
        app.button(key="ops_unlock").click().run()
        assert any("Incorrect token" in e.value for e in app.error)
        with pytest.raises(KeyError):
            app.button(key="run_live")
        app.text_input(key="ops_token_input").set_value("s3cret-token").run()
        app.button(key="ops_unlock").click().run()
        assert not app.exception
        assert app.button(key="run_live").disabled  # present now; still needs confirmation
    assert live.call_count == 0


def test_repeated_wrong_tokens_lock_the_session_out(market_factory):
    market = market_factory()
    app = run_page("operations", market.database, market.now, env=TOKEN_ENV)
    for _ in range(5):
        app.text_input(key="ops_token_input").set_value("wrong").run()
        app.button(key="ops_unlock").click().run()
    app.text_input(key="ops_token_input").set_value("s3cret-token").run()
    app.button(key="ops_unlock").click().run()
    assert any("Too many incorrect attempts" in e.value for e in app.error)
    with pytest.raises(KeyError):
        app.button(key="run_live")


def test_without_a_token_the_page_says_operations_are_unprotected(market_factory):
    market = market_factory()
    app = run_page("operations", market.database, market.now)
    text = " ".join(i.value for i in app.info)
    assert "not access-protected" in text and "PRICING_WATCH_OPS_TOKEN" in text
    assert "localhost" in text


def test_a_locked_session_cannot_start_a_job_even_by_calling_the_callback(
    market_factory, clean_runner
):
    """Defence in depth: the callbacks re-check authorisation, not just the page layout."""
    from streamlit.testing.v1 import AppTest

    market = market_factory()
    script = f"""
from balance_pricing_engine.dashboard.pages import operations
from balance_pricing_engine.dashboard.settings import load_settings
settings = load_settings({TOKEN_ENV!r}, database_path={str(market.database)!r})
operations._start_job("live", "run_live", settings)
operations._run_quick("create_backup", settings)
"""
    with (
        patch.object(actions, "run_live") as live,
        patch.object(actions, "create_backup") as backup,
    ):
        app = AppTest.from_string(script).run()
    assert not app.exception
    assert live.call_count == 0 and backup.call_count == 0
    assert not clean_runner.state().running and clean_runner.state().result is None


def test_the_attempt_cap_holds_across_sessions_not_just_within_one(market_factory):
    """Opening a new tab/session must not hand an attacker a fresh set of guesses."""
    market = market_factory()
    attacker = run_page("operations", market.database, market.now, env=TOKEN_ENV)
    for _ in range(5):
        attacker.text_input(key="ops_token_input").set_value("wrong").run()
        attacker.button(key="ops_unlock").click().run()

    new_session = run_page("operations", market.database, market.now, env=TOKEN_ENV)
    new_session.text_input(key="ops_token_input").set_value("s3cret-token").run()
    new_session.button(key="ops_unlock").click().run()  # even the right token is refused for now
    assert any("Too many incorrect attempts" in e.value for e in new_session.error)
    with pytest.raises(KeyError):
        new_session.button(key="run_live")


def test_the_lockout_ends_and_the_right_token_then_works(market_factory, fresh_unlock_limiter):
    clock = [100.0]
    limiter = guard.AttemptLimiter(max_failures=1, lockout_seconds=60, monotonic=lambda: clock[0])
    guard._UNLOCK_ATTEMPTS = limiter
    market = market_factory()
    app = run_page("operations", market.database, market.now, env=TOKEN_ENV)
    app.text_input(key="ops_token_input").set_value("wrong").run()
    app.button(key="ops_unlock").click().run()
    clock[0] += 61
    app.text_input(key="ops_token_input").set_value("s3cret-token").run()
    app.button(key="ops_unlock").click().run()
    assert app.button(key="run_live") is not None
