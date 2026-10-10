"""Safety tests for the Operations page: only an explicit click may start a collection."""

import threading
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from dashboard_harness import run_page

from balance_pricing_engine.dashboard import actions

DONE = datetime(2026, 10, 11, tzinfo=timezone.utc)


def ok(name, status="SUCCESS", message="done"):
    return actions.ActionResult(name, status, message, DONE)


@pytest.fixture(autouse=True)
def clean_runner():
    runner = actions.default_runner()
    runner.wait()
    runner.clear_result()
    yield runner
    runner.wait()
    runner.clear_result()


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
