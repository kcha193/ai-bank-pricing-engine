import dataclasses
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


def test_live_run_launches_the_scheduled_workflow_once_per_product(settings):
    calls = []
    result = actions.run_live(settings, runner=fake_runner(2, SUMMARY, calls), clock=clock)
    assert [c[0][c[0].index("--product") + 1] for c in calls] == ["home_loan", "term_deposit"]
    for command, kwargs in calls:
        assert command[1].endswith("scripts/collect_market_rates_workflow.py")
        assert "--render-javascript" in command and "--summary-file" in command
        assert command[command.index("--database") + 1] == settings.intelligence.database_path
        assert command[command.index("--config") + 1] == str(settings.engine_config_path)
        assert str(settings.repo_root / "src") in kwargs["env"]["PYTHONPATH"]
    # Each product writes its own summary: the workflow's per-product lock/summary contract.
    assert len({c[0][c[0].index("--summary-file") + 1] for c in calls}) == 2
    assert result.status == "PARTIAL_SUCCESS"
    assert "last-known observations are retained" in result.message
    assert "max_observation_age_hours" in result.message
    assert "marked stale" not in result.message
    assert [p.product_type for p in result.parts] == list(actions.ALL_PRODUCTS)
    for part in result.parts:
        assert {b.bank: b.ok for b in part.banks} == {"ASB": True, "ANZ": True, "Westpac": False}
        assert part.summary["changes_detected"] == 2


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


def test_exit_2_without_a_summary_is_a_failure_not_a_partial_success(settings):
    """Python itself exits 2 for 'can't open file' and argparse errors; a real partial run
    always leaves a summary, so exit 2 alone must not be read as 'some banks failed'."""
    result = actions.run_live(settings, runner=fake_runner(2, None), clock=clock)
    assert result.status == "FAILED"
    assert "some banks failed" not in result.message


def test_exit_2_with_a_summary_is_still_a_partial_success(settings):
    result = actions.run_live(settings, runner=fake_runner(2, SUMMARY), clock=clock)
    assert result.status == "PARTIAL_SUCCESS"


def test_a_missing_workflow_script_is_reported_before_anything_is_launched(settings, tmp_path):
    no_scripts = dataclasses.replace(settings, repo_root=tmp_path)
    calls = []
    result = actions.run_live(no_scripts, runner=fake_runner(0, SUMMARY, calls), clock=clock)
    assert calls == []
    assert result.status == "FAILED" and "collection script" in result.message.lower()


def test_dry_run_exit_2_without_a_result_is_a_failure(settings):
    result = actions.run_dry(
        settings, runner=fake_runner(2, None, flag="--result-file"), clock=clock
    )
    assert result.status == "FAILED"


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
    assert [c[0][c[0].index("--product") + 1] for c in calls] == ["home_loan", "term_deposit"]
    for command, _ in calls:
        assert "balance_pricing_engine.dashboard.run_worker" in command
        assert not any("collect_market_rates_workflow" in part for part in command)
    assert result.status == "SUCCESS" and "nothing was persisted" in result.message.lower()
    for part in result.parts:
        assert {b.bank for b in part.banks} == {"ASB", "ANZ", "Westpac"}


def per_product_runner(codes, summaries, calls=None, flag="--summary-file"):
    """A runner whose exit code and summary depend on the ``--product`` it was launched with."""

    def run(cmd, **kwargs):
        product = cmd[cmd.index("--product") + 1]
        if calls is not None:
            calls.append(product)
        if summaries.get(product) is not None:
            Path(cmd[cmd.index(flag) + 1]).write_text(json.dumps(summaries[product]))
        return subprocess.CompletedProcess(cmd, codes[product], stdout="", stderr="")

    return run


ALL_OK = {**SUMMARY, "banks_succeeded": ["ANZ", "ASB", "BNZ", "Kiwibank", "Westpac"]}
ALL_OK = {**ALL_OK, "banks_failed": [], "failures": []}
TD_FAILED = {
    "banks_succeeded": [],
    "banks_failed": ["ANZ", "ASB", "BNZ", "Kiwibank", "Westpac"],
    "failures": [
        {"bank": bank, "error_type": "ParserError", "error": "no rates"}
        for bank in ("ANZ", "ASB", "BNZ", "Kiwibank", "Westpac")
    ],
}


@pytest.mark.parametrize(
    ("home_loan", "term_deposit", "overall"),
    [
        ((0, ALL_OK), (1, TD_FAILED), "PARTIAL_SUCCESS"),
        ((1, TD_FAILED), (0, ALL_OK), "PARTIAL_SUCCESS"),
        ((0, ALL_OK), (2, SUMMARY), "PARTIAL_SUCCESS"),
        ((0, ALL_OK), (3, None), "PARTIAL_SUCCESS"),
        ((1, TD_FAILED), (1, TD_FAILED), "FAILED"),
        ((1, TD_FAILED), (3, None), "FAILED"),
        ((0, ALL_OK), (0, ALL_OK), "SUCCESS"),
    ],
)
def test_one_market_failing_is_never_reported_as_overall_success(
    settings, home_loan, term_deposit, overall
):
    runner = per_product_runner(
        {"home_loan": home_loan[0], "term_deposit": term_deposit[0]},
        {"home_loan": home_loan[1], "term_deposit": term_deposit[1]},
    )
    result = actions.run_live(settings, runner=runner, clock=clock)
    assert result.status == overall
    by_product = {part.product_type: part for part in result.parts}
    assert by_product[actions.ProductType.HOME_LOAN].status == actions._STATUS_BY_EXIT[home_loan[0]]
    assert (
        by_product[actions.ProductType.TERM_DEPOSIT].status
        == actions._STATUS_BY_EXIT[term_deposit[0]]
    )
    # The message names each market's own outcome.
    assert f"Home loans: {by_product[actions.ProductType.HOME_LOAN].status}" in result.message
    assert f"Term deposits: {by_product[actions.ProductType.TERM_DEPOSIT].status}" in result.message


def test_a_failed_term_deposit_market_keeps_its_own_bank_failures(settings):
    runner = per_product_runner(
        {"home_loan": 0, "term_deposit": 1}, {"home_loan": ALL_OK, "term_deposit": TD_FAILED}
    )
    result = actions.run_live(settings, runner=runner, clock=clock)
    loans, deposits = result.parts
    assert all(b.ok for b in loans.banks)
    assert [(b.bank, b.ok, b.detail) for b in deposits.banks] == [
        (bank, False, "ParserError") for bank in ("ANZ", "ASB", "BNZ", "Kiwibank", "Westpac")
    ]
    assert result.banks == ()  # never a merged list that loses which market a bank failed in


@pytest.mark.parametrize("action", ["run_live", "run_dry"])
@pytest.mark.parametrize(
    "products", [(actions.ProductType.TERM_DEPOSIT,), (actions.ProductType.HOME_LOAN,)]
)
def test_a_single_market_selection_launches_only_that_market(settings, action, products):
    calls = []
    flag = "--summary-file" if action == "run_live" else "--result-file"
    payload = ALL_OK if action == "run_live" else {"status": "SUCCESS", "summary": ALL_OK}
    runner = per_product_runner(
        {"home_loan": 0, "term_deposit": 0},
        {"home_loan": payload, "term_deposit": payload},
        calls,
        flag=flag,
    )
    result = getattr(actions, action)(settings, products=products, runner=runner, clock=clock)
    assert calls == [products[0].value]
    assert result.product_type is products[0] and result.parts == ()


def test_dry_run_covers_both_markets_and_reports_each(settings):
    calls = []
    runner = per_product_runner(
        {"home_loan": 0, "term_deposit": 2},
        {
            "home_loan": {"status": "SUCCESS", "summary": ALL_OK},
            "term_deposit": {"status": "PARTIAL_SUCCESS", "summary": SUMMARY},
        },
        calls,
        flag="--result-file",
    )
    result = actions.run_dry(settings, runner=runner, clock=clock)
    assert calls == ["home_loan", "term_deposit"]
    assert result.status == "PARTIAL_SUCCESS"
    assert "nothing was persisted" in result.message.lower()
    assert [p.status for p in result.parts] == ["SUCCESS", "PARTIAL_SUCCESS"]


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

    result = actions.send_test_notification(
        sink_factory=Leaky, clock=clock, cooldown=actions.Cooldown(0)
    )
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

    assert (
        actions.send_test_notification(
            sink_factory=Ok, clock=clock, cooldown=actions.Cooldown(0)
        ).status
        == "SUCCESS"
    )
    assert len(sent) == 1


def test_job_runner_is_single_flight():
    gate, runs = threading.Event(), []

    def slow():
        runs.append(1)
        gate.wait(2)
        return actions.ActionResult("live", "SUCCESS", "ok", NOW)

    runner = actions.JobRunner(cooldown_seconds=0)
    assert runner.submit("live", slow) is None
    refusal = runner.submit("live", slow)  # double click / a second browser tab
    assert refusal is not None and "already in progress" in refusal
    assert runner.state().running
    gate.set()
    runner.wait()
    assert runs == [1] and runner.state().result.status == "SUCCESS"
    assert runner.submit("live", slow) is None  # free again once finished
    runner.wait()


def test_a_crashing_job_becomes_a_failed_result_not_a_dead_runner():
    def crash():
        raise RuntimeError("secret internals")

    runner = actions.JobRunner(cooldown_seconds=0)
    runner.submit("live", crash)
    runner.wait()
    state = runner.state()
    assert not state.running and state.result.status == "FAILED"
    assert "secret internals" not in state.result.message
    assert runner.submit("live", crash) is None
    runner.wait()


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_job_runner_enforces_a_cooldown_between_runs():
    clock = FakeClock()
    runner = actions.JobRunner(cooldown_seconds=60, monotonic=clock)
    ok = lambda: actions.ActionResult("live", "SUCCESS", "ok", NOW)  # noqa: E731
    assert runner.submit("live", ok) is None
    runner.wait()
    clock.now += 20
    refusal = runner.submit("live", ok)
    assert refusal is not None and "wait" in refusal and "40" in refusal
    clock.now += 41
    assert runner.submit("live", ok) is None
    runner.wait()


def test_cooldown_applies_across_live_and_dry_runs():
    clock = FakeClock()
    runner = actions.JobRunner(cooldown_seconds=60, monotonic=clock)
    ok = lambda: actions.ActionResult("x", "SUCCESS", "ok", NOW)  # noqa: E731
    runner.submit("live", ok)
    runner.wait()
    assert runner.submit("dry_run", ok) is not None


def test_repeated_backup_clicks_cannot_rotate_out_older_backups(tmp_path):
    from datetime import timedelta

    from balance_pricing_engine.pricing_intelligence.store import ObservationStore

    database = tmp_path / "m.db"
    ObservationStore(database).close()
    settings = load_settings({}, database_path=str(database))
    settings = dataclasses.replace(settings, backup_dir=tmp_path / "backups")

    first = actions.create_backup(settings, clock=clock)
    assert first.status == "SUCCESS"
    for seconds in range(1, 60):  # fifty-nine more clicks in the following minute
        again = actions.create_backup(settings, clock=lambda s=seconds: NOW + timedelta(seconds=s))
        assert again.status == "RATE_LIMITED"
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1
    assert "minutes ago" in again.message and "older backups" in again.message

    later = actions.create_backup(settings, clock=lambda: NOW + timedelta(minutes=11))
    assert later.status == "SUCCESS"
    assert len(list((tmp_path / "backups").glob("*.db"))) == 2


def test_telegram_test_has_a_cooldown_so_it_cannot_spam_the_chat():
    sent = []

    class Ok:
        def send(self, alert):
            sent.append(alert)

    limiter = actions.Cooldown(30, monotonic=FakeClock())
    first = actions.send_test_notification(sink_factory=Ok, clock=clock, cooldown=limiter)
    second = actions.send_test_notification(sink_factory=Ok, clock=clock, cooldown=limiter)
    assert first.status == "SUCCESS" and second.status == "RATE_LIMITED"
    assert len(sent) == 1


def test_simultaneous_backup_requests_cannot_both_pass_the_rate_limit(tmp_path):
    """Check-then-write must be atomic: two requests racing the check yield one backup."""
    import balance_pricing_engine.dashboard.actions as actions_module
    from balance_pricing_engine.pricing_intelligence.backup import backup_database
    from balance_pricing_engine.pricing_intelligence.store import ObservationStore

    database = tmp_path / "m.db"
    ObservationStore(database).close()
    settings = dataclasses.replace(
        load_settings({}, database_path=str(database)), backup_dir=tmp_path / "backups"
    )
    inside_first_write, release = threading.Event(), threading.Event()

    def slow_backup(*args, **kwargs):
        inside_first_write.set()
        release.wait(5)  # the first request is mid-write while the second one arrives
        return backup_database(*args, **kwargs)

    results = []

    def request():
        results.append(actions.create_backup(settings, clock=clock).status)

    original = actions_module.backup_database
    actions_module.backup_database = slow_backup
    try:
        first = threading.Thread(target=request)
        first.start()
        assert inside_first_write.wait(5)
        second = threading.Thread(target=request)
        second.start()
        second.join(0.3)  # without a lock the second request runs now and also passes the check
        release.set()
        first.join(5)
        second.join(5)
    finally:
        actions_module.backup_database = original

    assert sorted(results) == ["RATE_LIMITED", "SUCCESS"]
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1
