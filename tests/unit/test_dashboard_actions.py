import json
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from balance_pricing_engine.dashboard import actions
from balance_pricing_engine.dashboard.settings import load_settings

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def clock():
    return NOW


def fake_runner(exit_code, summary=None, calls=None, flag="--summary-file"):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        if summary is not None:
            Path(cmd[cmd.index(flag) + 1]).write_text(json.dumps(summary))
        return subprocess.CompletedProcess(cmd, exit_code, stdout="", stderr="")

    return run


@pytest.fixture
def settings(tmp_path):
    return load_settings({}, database_path=str(tmp_path / "m.db"))


SUMMARY = {
    "banks_succeeded": ["ASB", "ANZ"],
    "banks_failed": ["Westpac"],
    "rates_accepted": 14,
    "changes_detected": 2,
    "suspicious_changes": 0,
    "failures": [{"bank": "Westpac", "error_type": "ConnectionError", "error": "boom"}],
}


def test_live_run_launches_the_scheduled_workflow_once_with_render_javascript(settings):
    calls = []
    result = actions.run_live(settings, runner=fake_runner(2, SUMMARY, calls), clock=clock)
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[1].endswith("scripts/collect_market_rates_workflow.py")
    assert "--render-javascript" in command and "--summary-file" in command
    assert command[command.index("--database") + 1] == settings.intelligence.database_path
    assert command[command.index("--config") + 1] == str(settings.engine_config_path)
    assert str(settings.repo_root / "src") in kwargs["env"]["PYTHONPATH"]
    assert result.status == "PARTIAL_SUCCESS"
    assert {b.bank: b.ok for b in result.banks} == {"ASB": True, "ANZ": True, "Westpac": False}
    assert result.summary["changes_detected"] == 2


def test_live_run_without_an_engine_config_runs_database_only(tmp_path):
    settings = load_settings(
        {"PRICING_WATCH_CONFIG": str(tmp_path / "missing.yaml")},
        database_path=str(tmp_path / "m.db"),
    )
    calls = []
    actions.run_live(settings, runner=fake_runner(0, SUMMARY, calls), clock=clock)
    assert "--config" not in calls[0][0]


@pytest.mark.parametrize(
    "code,status",
    [
        (0, "SUCCESS"),
        (1, "FAILED"),
        (2, "PARTIAL_SUCCESS"),
        (3, "ALREADY_RUNNING"),
        (9, "FAILED"),
    ],
)
def test_exit_codes_map_to_statuses(settings, code, status):
    assert (
        actions.run_live(settings, runner=fake_runner(code, SUMMARY), clock=clock).status == status
    )


def test_already_running_says_nothing_was_started(settings):
    result = actions.run_live(settings, runner=fake_runner(3, None), clock=clock)
    assert "nothing was started" in result.message.lower()


def test_unreadable_summary_still_reports_the_exit_status(settings):
    result = actions.run_live(settings, runner=fake_runner(0, None), clock=clock)
    assert result.status == "SUCCESS" and "summary could not be read" in result.message.lower()


def test_timeout_is_reported_not_raised(settings):
    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    assert actions.run_live(settings, runner=hang, clock=clock).status == "TIMED_OUT"


def test_a_launch_failure_is_an_operational_message(settings, caplog):
    def broken(cmd, **kwargs):
        raise FileNotFoundError("no python")

    result = actions.run_live(settings, runner=broken, clock=clock)
    assert result.status == "FAILED" and "no python" not in result.message
    assert any(r.exc_info for r in caplog.records)


def test_dry_run_uses_the_worker_and_never_the_workflow_script(settings):
    calls = []
    payload = {"status": "SUCCESS", "summary": SUMMARY}
    result = actions.run_dry(
        settings, runner=fake_runner(0, payload, calls, flag="--result-file"), clock=clock
    )
    command = calls[0][0]
    assert "balance_pricing_engine.dashboard.run_worker" in command
    assert not any("collect_market_rates_workflow" in part for part in command)
    assert result.status == "SUCCESS" and "nothing was persisted" in result.message.lower()
    assert {b.bank for b in result.banks} == {"ASB", "ANZ", "Westpac"}


def test_backup_reports_the_location_inside_the_backup_directory(tmp_path):
    from balance_pricing_engine.pricing_intelligence.store import ObservationStore

    database = tmp_path / "m.db"
    ObservationStore(database).close()
    settings = load_settings({}, database_path=str(database))
    settings = settings.__class__(**{**settings.__dict__, "backup_dir": tmp_path / "backups"})
    result = actions.create_backup(settings, clock=clock)
    assert result.status == "SUCCESS" and Path(result.summary["path"]).exists()
    assert result.summary["path"].startswith(str(tmp_path / "backups"))


def test_backup_without_a_database_fails_gracefully(tmp_path):
    settings = load_settings({}, database_path=str(tmp_path / "x.db"))
    result = actions.create_backup(settings, clock=clock)
    assert result.status == "FAILED" and "no database" in result.message.lower()


def test_telegram_not_configured_is_explained_without_naming_credentials():
    result = actions.send_test_notification(sink_factory=lambda: None, clock=clock)
    assert result.status == "NOT_CONFIGURED"
    assert result.message == "Telegram is not configured for this environment."


def test_telegram_failure_never_leaks_the_bot_token(caplog):
    class Leaky:
        def send(self, alert):
            raise RuntimeError("POST https://api.telegram.org/bot123456:SECRET/sendMessage failed")

    result = actions.send_test_notification(sink_factory=Leaky, clock=clock)
    assert result.status == "FAILED"
    assert "SECRET" not in result.message
    assert caplog.records, "the failure should be logged (class name only)"
    assert all("SECRET" not in r.getMessage() for r in caplog.records)
    assert all(r.exc_info is None for r in caplog.records)  # no traceback carrying the URL


def test_telegram_success_sends_exactly_one_message():
    sent = []

    class Ok:
        def send(self, alert):
            sent.append(alert)

    assert actions.send_test_notification(sink_factory=Ok, clock=clock).status == "SUCCESS"
    assert len(sent) == 1


def test_job_runner_is_single_flight():
    gate, runs = threading.Event(), []

    def slow():
        runs.append(1)
        gate.wait(2)
        return actions.ActionResult("live", "SUCCESS", "ok", NOW)

    runner = actions.JobRunner()
    assert runner.submit("live", slow) is True
    assert runner.submit("live", slow) is False  # double click / a second browser tab
    assert runner.state().running
    gate.set()
    runner.wait()
    assert runs == [1] and runner.state().result.status == "SUCCESS"
    assert runner.submit("live", slow) is True  # free again once finished
    runner.wait()


def test_a_crashing_job_becomes_a_failed_result_not_a_dead_runner():
    def crash():
        raise RuntimeError("secret internals")

    runner = actions.JobRunner()
    runner.submit("live", crash)
    runner.wait()
    state = runner.state()
    assert not state.running and state.result.status == "FAILED"
    assert "secret internals" not in state.result.message
    assert runner.submit("live", crash) is True
    runner.wait()
