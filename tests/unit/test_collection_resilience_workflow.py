"""Collection deadlines, process supervision, lock-error summaries and exit-3
escalation for the scheduled workflow (DATA-008, DATA-009).

Deadline logic is pinned two ways: fake-clock unit tests for the in-process
(soft) deadline, and real child processes for the supervisor's hard deadline --
a fake clock alone cannot prove that a hung process is actually terminated.
No test contacts a bank site.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import textwrap
import threading
import time

import pytest

from balance_pricing_engine.competitor_rates.collectors import (
    BankCollectionResult,
    CollectionStatus,
)
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.competitor_rates.run_deadline import (
    CollectionTimeoutError,
    RunDeadline,
)
from balance_pricing_engine.competitor_rates.collection_config import (
    estimated_worst_case_run_seconds,
)
from balance_pricing_engine.pricing_intelligence.alerts import Alert
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
    SkipEscalationPolicy,
)
from balance_pricing_engine.pricing_intelligence.locking import SingleInstanceLock
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle
from balance_pricing_engine.pricing_intelligence.run_artifacts import write_json_atomically
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.skip_escalation import (
    SKIP_ALERT,
    SKIP_NONE,
    SKIP_WARNING,
    record_lock_acquired,
    record_skip,
    skip_state_path,
)
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_intelligence.supervisor import run_supervised

NOW = datetime.now(timezone.utc) - timedelta(hours=1)
PYTHON = sys.executable


def load_script():
    path = Path("scripts/collect_market_rates_workflow.py")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workflow():
    return load_script()


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class StubCollector:
    def __init__(self, bank, record, on_collect=None):
        self.bank = bank
        self.record = record
        self.on_collect = on_collect
        self.calls = 0

    def collect_mortgage_rates(self, captured_at_utc=None, persist_snapshot=True):
        self.calls += 1
        if self.on_collect is not None:
            self.on_collect()
        return BankCollectionResult(
            bank=self.bank,
            product_type=ProductType.HOME_LOAN,
            status=CollectionStatus.SUCCESS,
            source_url=f"https://www.example-bank.co.nz/{self.bank.lower()}",
            records=(self.record.model_copy(update={"bank": self.bank}),),
        )

    collect_term_deposit_rates = collect_mortgage_rates


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - pid reused by another user's process
        return True
    return True


def _wait_dead(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        try:  # reap if it is our own child
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.05)
    return not _alive(pid)


def _operational_runs(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM operational_runs")]
    finally:
        connection.close()


# ------------------------------------------------------------ deadline budget


def test_default_run_deadline_exceeds_the_computed_worst_case_for_five_banks():
    config = CollectionConfig()
    worst = estimated_worst_case_run_seconds(config, bank_count=5)
    assert worst == pytest.approx(1264.0)
    assert config.collection_run_timeout_seconds > worst
    # ...but stays well inside the daily schedule interval.
    assert config.collection_run_timeout_seconds <= 3600


def test_run_deadline_uses_the_injected_monotonic_clock():
    clock = FakeClock()
    deadline = RunDeadline(10, clock=clock)
    clock.now += 9.5
    deadline.check("bank ANZ")
    assert deadline.remaining == pytest.approx(0.5)
    clock.now += 1
    with pytest.raises(CollectionTimeoutError) as caught:
        deadline.check("bank ASB")
    assert caught.value.stage == "bank ASB"
    assert caught.value.elapsed_seconds == pytest.approx(10.5)
    assert caught.value.timeout_seconds == 10


def test_run_deadline_rejects_non_positive_or_non_finite_budgets():
    for invalid in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            RunDeadline(invalid)


def test_t09_deadline_stops_further_banks_and_writes_nothing(tmp_path, make_record):
    """Fake clock: the second bank overruns; the third is never fetched and nothing persists."""
    clock = FakeClock()
    deadline = RunDeadline(30, clock=clock)

    def slow():
        clock.now += 31

    collectors = [
        StubCollector("ANZ", make_record()),
        StubCollector("ASB", make_record(), on_collect=slow),
        StubCollector("BNZ", make_record()),
    ]
    path = tmp_path / "observations.db"
    config = PricingIntelligenceConfig(database_path=str(path))
    with PricingIntelligenceService(
        config=config, store=ObservationStore(path), collectors=collectors
    ) as service:
        with pytest.raises(CollectionTimeoutError) as caught:
            run_operational_cycle(
                service, captured_at_utc=NOW, alert_sinks={}, deadline=deadline
            )
    assert caught.value.stage == "before BNZ"
    assert collectors[2].calls == 0
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
    connection.close()


def test_t09_a_run_inside_its_deadline_completes_normally(tmp_path, make_record):
    clock = FakeClock()
    deadline = RunDeadline(30, clock=clock)
    path = tmp_path / "observations.db"
    with PricingIntelligenceService(
        config=PricingIntelligenceConfig(database_path=str(path)),
        store=ObservationStore(path),
        collectors=[StubCollector(bank, make_record()) for bank in ("ANZ", "ASB", "BNZ")],
    ) as service:
        result = run_operational_cycle(
            service, captured_at_utc=NOW, alert_sinks={}, deadline=deadline
        )
    assert result.code == 0


def test_deadline_expiring_after_fetch_blocks_the_persist(tmp_path, make_record):
    """H: no observations are written once the run is classified as timed out."""
    clock = FakeClock()
    deadline = RunDeadline(30, clock=clock)

    def overrun():
        clock.now += 31

    path = tmp_path / "observations.db"
    with PricingIntelligenceService(
        config=PricingIntelligenceConfig(database_path=str(path)),
        store=ObservationStore(path),
        collectors=[StubCollector("ANZ", make_record(), on_collect=overrun)],
    ) as service:
        with pytest.raises(CollectionTimeoutError) as caught:
            service.collect(captured_at_utc=NOW, deadline=deadline)
    assert caught.value.stage == "persist"
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
    connection.close()


def test_in_process_timeout_writes_summary_and_a_failed_run_record(
    workflow, tmp_path, make_record, monkeypatch
):
    clock = FakeClock()

    def overrun():
        clock.now += 1000

    monkeypatch.setattr(
        "balance_pricing_engine.pricing_intelligence.service.build_collectors",
        lambda **kwargs: [StubCollector("ANZ", make_record(), on_collect=overrun)],
    )
    path = tmp_path / "observations.db"
    summary_path = tmp_path / "summary.json"
    config = PricingIntelligenceConfig(database_path=str(path), selected_banks=["ANZ"])
    code, summary = workflow.run_collection(
        config,
        render_javascript=False,
        summary_path=summary_path,
        run_id="run-timeout-1",
        deadline=RunDeadline(60, clock=clock),
    )
    assert code == workflow.EXIT_TOTAL_FAILURE
    written = json.loads(summary_path.read_text())
    assert written == summary
    assert written["failure_type"] == "collection_timeout"
    assert written["error_type"] == "collection_timeout"
    assert written["run_id"] == "run-timeout-1"
    assert written["product_type"] == "home_loan"
    assert written["timeout_seconds"] == 60
    assert written["elapsed_seconds"] >= 1000
    assert written["run_record_persisted"] is True
    runs = _operational_runs(path)
    assert [(run["run_id"], run["status"]) for run in runs] == [("run-timeout-1", "FAILED")]
    assert "collection_timeout" in runs[0]["error_summary"]


# --------------------------------------------------------- hard supervision


def test_t10_a_hung_worker_is_forcibly_terminated(tmp_path):
    started = time.monotonic()
    result = run_supervised(
        [PYTHON, "-c", "import time; time.sleep(120)"],
        timeout_seconds=1.0,
        grace_seconds=1.0,
    )
    assert result.timed_out
    assert result.returncode != 0
    assert time.monotonic() - started < 10
    assert not _alive(result.pid)


def test_a_worker_ignoring_sigterm_is_killed_after_the_grace_period(tmp_path):
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"
    result = run_supervised([PYTHON, "-c", script], timeout_seconds=0.5, grace_seconds=0.5)
    assert result.timed_out
    assert result.killed_after_grace
    assert not _alive(result.pid)


def test_t11_detached_browser_style_grandchildren_are_cleaned_up(tmp_path):
    """Playwright launches Chromium in its own session; a process-group kill misses it."""
    pid_file = tmp_path / "grandchild.pid"
    script = textwrap.dedent(
        f"""
        import subprocess, sys, time
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
        )
        open({str(pid_file)!r}, "w").write(str(child.pid))
        time.sleep(120)
        """
    )
    result = run_supervised([PYTHON, "-c", script], timeout_seconds=2.0, grace_seconds=1.0)
    assert result.timed_out
    grandchild = int(pid_file.read_text())
    assert grandchild in result.terminated_pids
    assert _wait_dead(grandchild), "detached grandchild survived the supervisor"


def test_t15_a_timed_out_worker_cannot_write_afterwards(tmp_path):
    database = tmp_path / "late.db"
    script = textwrap.dedent(
        f"""
        import sqlite3, time
        time.sleep(3)
        connection = sqlite3.connect({str(database)!r})
        connection.execute("CREATE TABLE late_write (x)")
        connection.commit()
        """
    )
    result = run_supervised([PYTHON, "-c", script], timeout_seconds=0.5, grace_seconds=0.5)
    assert result.timed_out
    time.sleep(3.5)
    if database.exists():
        connection = sqlite3.connect(database)
        tables = connection.execute("SELECT name FROM sqlite_master").fetchall()
        connection.close()
        assert ("late_write",) not in tables


def test_a_worker_finishing_in_time_returns_its_own_exit_code():
    result = run_supervised([PYTHON, "-c", "raise SystemExit(2)"], timeout_seconds=30)
    assert not result.timed_out
    assert result.returncode == 2


def _hanging_worker(monkeypatch, workflow, seconds=120):
    def command(argv, *, run_id, soft_deadline_seconds):
        return [PYTHON, "-c", f"import time; time.sleep({seconds})"]

    monkeypatch.setattr(workflow, "_worker_command", command)


def test_t12_t13_t14_supervised_timeout_exit_summary_record_and_lock(
    workflow, tmp_path, monkeypatch
):
    _hanging_worker(monkeypatch, workflow)
    database = tmp_path / "observations.db"
    lock = tmp_path / "collection.lock"
    summary_path = tmp_path / "summary.json"
    started = time.monotonic()
    code = workflow.main(
        [
            "--database", str(database), "--lock-file", str(lock),
            "--summary-file", str(summary_path), "--quiet",
            "--run-timeout-seconds", "1", "--run-id", "supervised-1",
        ]
    )
    assert code == workflow.EXIT_TOTAL_FAILURE
    assert time.monotonic() - started < 30
    summary = json.loads(summary_path.read_text())
    assert summary["failure_type"] == "collection_timeout"
    assert summary["run_id"] == "supervised-1"
    assert summary["product_type"] == "home_loan"
    assert summary["timeout_seconds"] == 1
    assert summary["elapsed_seconds"] >= 1
    assert summary["supervisor"]["worker_terminated"] is True
    assert summary["run_record_persisted"] is True
    assert summary["banks_failed"] == ["*"]
    assert [(run["run_id"], run["status"]) for run in _operational_runs(database)] == [
        ("supervised-1", "FAILED")
    ]
    with SingleInstanceLock(lock):  # T14: released
        pass


def test_supervised_timeout_reports_an_unpersisted_record_when_the_db_is_locked(
    workflow, tmp_path, monkeypatch
):
    _hanging_worker(monkeypatch, workflow)
    database = tmp_path / "observations.db"
    ObservationStore(database).close()
    blocker = sqlite3.connect(database, timeout=0)
    blocker.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(workflow, "SUPERVISOR_RECORD_BUSY_TIMEOUT_SECONDS", 0.2)
    try:
        code = workflow.main(
            [
                "--database", str(database), "--lock-file", str(tmp_path / "c.lock"),
                "--summary-file", str(tmp_path / "summary.json"), "--quiet",
                "--run-timeout-seconds", "1",
            ]
        )
    finally:
        blocker.rollback()
        blocker.close()
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert code == workflow.EXIT_TOTAL_FAILURE
    assert summary["failure_type"] == "collection_timeout"
    assert summary["run_record_persisted"] is False
    assert "database_locked" in summary["run_record_note"]
    assert _operational_runs(database) == []


def test_a_crashed_worker_still_leaves_a_classified_summary(workflow, tmp_path, monkeypatch):
    monkeypatch.setattr(
        workflow,
        "_worker_command",
        lambda argv, *, run_id, soft_deadline_seconds: [PYTHON, "-c", "raise SystemExit(70)"],
    )
    summary_path = tmp_path / "summary.json"
    code = workflow.main(
        [
            "--database", str(tmp_path / "o.db"), "--lock-file", str(tmp_path / "c.lock"),
            "--summary-file", str(summary_path), "--quiet", "--run-timeout-seconds", "30",
        ]
    )
    assert code == workflow.EXIT_TOTAL_FAILURE
    summary = json.loads(summary_path.read_text())
    assert summary["failure_type"] == "worker_failed"
    assert summary["supervisor"]["worker_exit_code"] == 70


def test_supervised_real_worker_round_trip(workflow, tmp_path):
    """The real worker entry point runs under the supervisor and its summary/exit pass through.

    No bank is selected, so nothing is fetched: the run is a classified total failure.
    """
    import yaml

    config_path = tmp_path / "config.yaml"
    base = yaml.safe_load(Path("configs/base.yaml").read_text())
    base["pricing_intelligence"] = {"selected_banks": []}
    config_path.write_text(yaml.safe_dump(base))
    summary_path = tmp_path / "summary.json"
    code = workflow.main(
        [
            "--config", str(config_path), "--database", str(tmp_path / "o.db"),
            "--lock-file", str(tmp_path / "c.lock"), "--summary-file", str(summary_path),
            "--quiet", "--run-timeout-seconds", "120", "--run-id", "round-trip",
        ]
    )
    summary = json.loads(summary_path.read_text())
    assert code == workflow.EXIT_TOTAL_FAILURE
    assert summary["run_id"] == "round-trip"
    assert "supervisor" not in summary
    assert summary["rates_accepted"] == 0


# ----------------------------------------------------------- atomic summaries


def test_summaries_are_replaced_atomically(tmp_path, monkeypatch):
    target = tmp_path / "summary.json"
    target.write_text('{"old": true}')

    def broken_replace(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        write_json_atomically(target, {"new": True})
    assert json.loads(target.read_text()) == {"old": True}
    assert list(tmp_path.glob("*.tmp")) == []


# ------------------------------------------------------- database lock (T27)


def test_t27_a_database_lock_produces_a_classified_summary(
    workflow, tmp_path, make_record, monkeypatch
):
    path = tmp_path / "observations.db"
    holder: dict[str, sqlite3.Connection] = {}

    def grab_lock():
        blocker = sqlite3.connect(path, timeout=0, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        holder["blocker"] = blocker

    monkeypatch.setattr(
        "balance_pricing_engine.pricing_intelligence.service.build_collectors",
        lambda **kwargs: [StubCollector("ANZ", make_record(), on_collect=grab_lock)],
    )
    summary_path = tmp_path / "summary.json"
    config = PricingIntelligenceConfig(
        database_path=str(path), selected_banks=["ANZ"], sqlite_busy_timeout_seconds=0.2
    )
    try:
        code, summary = workflow.run_collection(
            config, render_javascript=False, summary_path=summary_path, run_id="locked-1"
        )
    finally:
        holder["blocker"].rollback()
        holder["blocker"].close()
    assert code == workflow.EXIT_TOTAL_FAILURE
    written = json.loads(summary_path.read_text())
    assert written["error_type"] == "database_locked"
    assert written["failure_type"] == "database_locked"
    assert written["run_record_persisted"] is False
    assert written["run_id"] == "locked-1"
    assert written["failures"][0]["error_type"] == "database_locked"
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
    connection.close()


def test_a_non_lock_database_error_keeps_its_own_classification(workflow, tmp_path):
    bogus = tmp_path / "corrupt.db"
    bogus.write_bytes(b"not a database" * 200)
    summary_path = tmp_path / "summary.json"
    code, summary = workflow.run_collection(
        PricingIntelligenceConfig(database_path=str(bogus), selected_banks=["ANZ"]),
        render_javascript=False,
        summary_path=summary_path,
    )
    assert code == workflow.EXIT_TOTAL_FAILURE
    assert summary["error_type"] == "DatabaseError"
    assert summary["failure_type"] == "database_error"


# ------------------------------------------------------ exit-3 escalation


class RecordingSink:
    def __init__(self):
        self.alerts: list[Alert] = []

    def send(self, alert):
        self.alerts.append(alert)


class FailingSink:
    def send(self, alert):
        raise ConnectionError("unreachable")


POLICY = SkipEscalationPolicy(warning_after_consecutive_skips=2, alert_after_consecutive_skips=3)


def _skip(lock, invocation, sinks, product=ProductType.HOME_LOAN):
    return record_skip(
        skip_state_path(lock),
        product_type=product,
        invocation_id=invocation,
        policy=POLICY,
        sinks=sinks,
        now=NOW,
    )


def test_t16_distinct_blocked_attempts_advance_the_streak(tmp_path):
    lock = tmp_path / "collection-home-loan.lock"
    levels = [_skip(lock, f"run-{index}", {}).level for index in range(4)]
    assert levels == [SKIP_NONE, SKIP_WARNING, SKIP_ALERT, SKIP_ALERT]
    state = json.loads(skip_state_path(lock).read_text())
    assert state["consecutive_skips"] == 4
    assert state["product_type"] == "home_loan"


def test_the_same_invocation_is_not_counted_twice(tmp_path):
    lock = tmp_path / "collection-home-loan.lock"
    first = _skip(lock, "same-run", {})
    again = _skip(lock, "same-run", {})
    assert first.consecutive_skips == again.consecutive_skips == 1
    assert again.counted is False


def test_t17_product_streaks_are_independent(tmp_path):
    home = tmp_path / "collection-home-loan.lock"
    deposits = tmp_path / "collection-term-deposit.lock"
    for index in range(3):
        _skip(home, f"hl-{index}", {})
    outcome = _skip(deposits, "td-0", {}, product=ProductType.TERM_DEPOSIT)
    assert outcome.consecutive_skips == 1
    assert outcome.level == SKIP_NONE


def test_t18_t19_threshold_alerts_once_per_streak_and_channel(tmp_path):
    lock = tmp_path / "collection-home-loan.lock"
    sink = RecordingSink()
    outcomes = [_skip(lock, f"run-{index}", {"telegram": sink}) for index in range(5)]
    assert [outcome.alerts_sent for outcome in outcomes] == [0, 0, 1, 0, 0]
    assert len(sink.alerts) == 1
    assert "3 consecutive" in sink.alerts[0].body
    assert "home loan" in sink.alerts[0].title.lower()


def test_an_undelivered_skip_alert_is_retried_on_the_next_skip_only_for_that_channel(tmp_path):
    lock = tmp_path / "collection-home-loan.lock"
    good = RecordingSink()
    for index in range(3):
        _skip(lock, f"run-{index}", {"webhook": FailingSink(), "telegram": good})
    later = RecordingSink()
    outcome = _skip(lock, "run-3", {"webhook": later, "telegram": good})
    assert len(good.alerts) == 1
    assert len(later.alerts) == 1
    assert outcome.alerts_sent == 1


def test_t20_acquiring_the_lock_resets_the_streak_and_notifies_alerted_channels(tmp_path):
    lock = tmp_path / "collection-home-loan.lock"
    sink = RecordingSink()
    for index in range(3):
        _skip(lock, f"run-{index}", {"telegram": sink})
    recovery = record_lock_acquired(
        skip_state_path(lock), product_type=ProductType.HOME_LOAN, sinks={"telegram": sink},
        now=NOW,
    )
    assert recovery.recovered and recovery.previous_streak == 3
    assert recovery.alerts_sent == 1
    assert "recovered" in sink.alerts[-1].title.lower()
    assert _skip(lock, "run-9", {}).consecutive_skips == 1


def test_recovery_from_a_short_streak_sends_nothing(tmp_path):
    lock = tmp_path / "collection-home-loan.lock"
    sink = RecordingSink()
    _skip(lock, "run-0", {"telegram": sink})
    recovery = record_lock_acquired(
        skip_state_path(lock), product_type=ProductType.HOME_LOAN, sinks={"telegram": sink},
        now=NOW,
    )
    assert recovery.recovered and recovery.alerts_sent == 0
    assert sink.alerts == []


def test_a_corrupt_skip_sidecar_restarts_the_count_instead_of_crashing(tmp_path, caplog):
    lock = tmp_path / "collection-home-loan.lock"
    skip_state_path(lock).write_text("{not json")
    outcome = _skip(lock, "run-0", {})
    assert outcome.consecutive_skips == 1
    assert "unreadable" in caplog.text


def test_main_counts_scheduled_skips_and_escalates(workflow, tmp_path, caplog):
    lock = tmp_path / "collection-home-loan.lock"
    args = ["--database", str(tmp_path / "o.db"), "--lock-file", str(lock), "--quiet"]
    with SingleInstanceLock(lock):
        codes = [workflow.main(args) for _ in range(3)]
    assert codes == [workflow.EXIT_ALREADY_RUNNING] * 3
    state = json.loads(skip_state_path(lock).read_text())
    assert state["consecutive_skips"] == 3
    assert "collection skip escalation" in caplog.text


def test_t16_skip_with_a_repeated_run_id_is_not_double_counted_by_main(workflow, tmp_path):
    lock = tmp_path / "collection-home-loan.lock"
    args = [
        "--database", str(tmp_path / "o.db"), "--lock-file", str(lock), "--quiet",
        "--run-id", "retry-me",
    ]
    with SingleInstanceLock(lock):
        workflow.main(args)
        workflow.main(args)
    assert json.loads(skip_state_path(lock).read_text())["consecutive_skips"] == 1


def test_skip_alerts_never_contain_channel_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:SECRET-token-value")
    monkeypatch.setenv("PRICING_INTELLIGENCE_ALERT_WEBHOOK_URL", "https://hooks.example/SECRET")
    lock = tmp_path / "collection-home-loan.lock"
    sink = RecordingSink()
    for index in range(3):
        _skip(lock, f"run-{index}", {"telegram": sink})
    text = sink.alerts[0].title + sink.alerts[0].body + skip_state_path(lock).read_text()
    assert "SECRET" not in text


# -------------------------------------------------- product isolation (T37)


def test_t37_home_loan_and_deposit_runs_share_the_database_safely(tmp_path, make_record):
    path = tmp_path / "observations.db"
    ObservationStore(path).close()
    results: dict[str, int] = {}
    errors: list[BaseException] = []

    def run(product: ProductType):
        try:
            record = make_record(
                product_type=product.value,
                rate_category="term_deposit" if product is ProductType.TERM_DEPOSIT else "special",
            )
            with PricingIntelligenceService(
                config=PricingIntelligenceConfig(database_path=str(path)),
                store=ObservationStore(path, busy_timeout_seconds=10),
                collectors=[StubCollector(bank, record) for bank in ("ANZ", "ASB", "BNZ")],
            ) as service:
                results[product.value] = run_operational_cycle(
                    service, product_type=product, captured_at_utc=NOW, alert_sinks={}
                ).code
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(product,)) for product in ProductType]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert results == {"home_loan": 0, "term_deposit": 0}
    runs = _operational_runs(path)
    assert sorted(run["product_type"] for run in runs) == ["home_loan", "term_deposit"]
    assert len({run["run_id"] for run in runs}) == 2


def test_a_deposit_timeout_never_marks_home_loans_failed(workflow, tmp_path, make_record, monkeypatch):
    path = tmp_path / "observations.db"
    with PricingIntelligenceService(
        config=PricingIntelligenceConfig(database_path=str(path)),
        store=ObservationStore(path),
        collectors=[StubCollector(bank, make_record()) for bank in ("ANZ", "ASB", "BNZ")],
    ) as service:
        run_operational_cycle(service, captured_at_utc=NOW, alert_sinks={})

    clock = FakeClock()

    def overrun():
        clock.now += 1000

    monkeypatch.setattr(
        "balance_pricing_engine.pricing_intelligence.service.build_collectors",
        lambda **kwargs: [StubCollector("ANZ", make_record(), on_collect=overrun)],
    )
    workflow.run_collection(
        PricingIntelligenceConfig(database_path=str(path)),
        render_javascript=False,
        product_type=ProductType.TERM_DEPOSIT,
        deadline=RunDeadline(60, clock=clock),
    )
    with ObservationStore(path) as store:
        latest_home = store.get_latest_run_record(ProductType.HOME_LOAN)
        latest_deposit = store.get_latest_run_record(ProductType.TERM_DEPOSIT)
    assert latest_home.status == "SUCCESS"
    assert latest_deposit.status == "FAILED"


# ------------------------------------------------------------- idempotency (T38)


def test_t38_retrying_a_run_id_after_a_timeout_record_stays_idempotent(
    workflow, tmp_path, make_record, monkeypatch
):
    clock = FakeClock()

    def overrun():
        clock.now += 1000

    monkeypatch.setattr(
        "balance_pricing_engine.pricing_intelligence.service.build_collectors",
        lambda **kwargs: [StubCollector("ANZ", make_record(), on_collect=overrun)],
    )
    path = tmp_path / "observations.db"
    config = PricingIntelligenceConfig(database_path=str(path), selected_banks=["ANZ"])
    for _ in range(2):
        workflow.run_collection(
            config, render_javascript=False, run_id="same-run",
            deadline=RunDeadline(60, clock=clock),
        )
    assert [(run["run_id"], run["status"]) for run in _operational_runs(path)] == [
        ("same-run", "FAILED")
    ]


def test_retry_summary_preserves_original_committed_evidence(
    workflow, tmp_path, make_record, monkeypatch
):
    collector = StubCollector("ANZ", make_record())
    monkeypatch.setattr(
        "balance_pricing_engine.pricing_intelligence.service.build_collectors",
        lambda **kwargs: [collector],
    )
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "market.db"), selected_banks=["ANZ"]
    )
    code, original = workflow.run_collection(config, False, run_id="replayed")
    assert code == 0 and original["rates_accepted"] == 1

    def failed(**kwargs):
        return BankCollectionResult(
            bank="ANZ",
            product_type=ProductType.HOME_LOAN,
            status=CollectionStatus.FAILED,
            source_url="https://example.com/rates",
            error_type="FetchError",
            error="offline failure",
        )

    monkeypatch.setattr(collector, "collect_mortgage_rates", failed)
    code, retry = workflow.run_collection(config, False, run_id="replayed")
    assert code == 0 and retry["status"] == "SUCCESS"
    assert retry["banks_succeeded"] == ["ANZ"]
    assert retry["banks_failed"] == []
    assert retry["rates_accepted"] == 1
    assert retry["already_recorded"] is True
    assert retry["retry_fetch"]["banks_failed"] == ["ANZ"]


def test_database_failure_after_collection_preserves_commit_evidence(
    workflow, tmp_path, make_record, monkeypatch
):
    monkeypatch.setattr(
        "balance_pricing_engine.pricing_intelligence.service.build_collectors",
        lambda **kwargs: [StubCollector("ANZ", make_record())],
    )

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ObservationStore, "write_run_record", locked)
    path = tmp_path / "market.db"
    config = PricingIntelligenceConfig(database_path=str(path), selected_banks=["ANZ"])
    code, summary = workflow.run_collection(config, False, run_id="committed-before-error")
    assert code == 1 and summary["failure_type"] == "database_locked"
    with sqlite3.connect(path) as reader:
        assert reader.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
    assert "no observations were committed" not in summary["run_record_note"]
    assert summary["observations_committed"] is True
    assert summary["rates_accepted"] == 1


def test_replayed_partial_run_keeps_original_failure_evidence(
    workflow, tmp_path, make_record, monkeypatch
):
    good = StubCollector("ANZ", make_record())
    bad = StubCollector("BNZ", make_record())
    normal = bad.collect_mortgage_rates

    def failed(**kwargs):
        return BankCollectionResult(
            bank="BNZ",
            product_type=ProductType.HOME_LOAN,
            status=CollectionStatus.FAILED,
            source_url="https://example.com/rates",
            error_type="FetchError",
            error="offline failure",
        )

    monkeypatch.setattr(bad, "collect_mortgage_rates", failed)
    monkeypatch.setattr(
        "balance_pricing_engine.pricing_intelligence.service.build_collectors",
        lambda **kwargs: [good, bad],
    )
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "market.db"), selected_banks=["ANZ", "BNZ"]
    )
    assert workflow.run_collection(config, False, run_id="partial-replay")[0] == 2
    monkeypatch.setattr(bad, "collect_mortgage_rates", normal)
    code, retry = workflow.run_collection(config, False, run_id="partial-replay")
    assert code == 2 and retry["status"] == "PARTIAL_SUCCESS"
    assert retry["banks_failed"] == ["BNZ"]
    assert retry["retry_fetch"]["banks_failed"] == []
    assert retry["validation_warnings"] is None  # original details cannot be reconstructed


def test_replay_without_original_run_record_is_incomplete_not_successful(
    workflow, tmp_path, make_record, monkeypatch
):
    monkeypatch.setattr(
        "balance_pricing_engine.pricing_intelligence.service.build_collectors",
        lambda **kwargs: [StubCollector("ANZ", make_record())],
    )
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "market.db"), selected_banks=["ANZ"]
    )
    with PricingIntelligenceService(config=config) as service:
        service.collect(run_id="crashed-after-commit")
    code, summary = workflow.run_collection(config, False, run_id="crashed-after-commit")
    assert code == 1 and summary["failure_type"] == "incomplete_run_record"
    assert summary["observations_committed"] is True
    assert summary["run_record_persisted"] is False
    assert summary["rates_accepted"] is None
    assert _operational_runs(config.database_path) == []


@pytest.mark.parametrize("product", [ProductType.HOME_LOAN, ProductType.TERM_DEPOSIT])
def test_postcommit_timeout_records_failed_attempt_and_degrades_health(
    dual_market_factory, make_record, product
):
    from balance_pricing_engine.pricing_intelligence.health import (
        OperationalState,
        RunHealth,
        assess_system_health,
        run_health,
    )
    from balance_pricing_engine.pricing_intelligence.orchestration import record_failed_attempt

    market = dual_market_factory()
    when = market.now + timedelta(minutes=1)
    with PricingIntelligenceService(config=market.config) as service:
        other = (
            ProductType.TERM_DEPOSIT if product is ProductType.HOME_LOAN else ProductType.HOME_LOAN
        )
        other_before = service.store.get_latest_run_record(other)
        assert run_health(service.store.get_latest_run_record(product)) is RunHealth.HEALTHY
        record = make_record(
            bank="ANZ",
            product_type=product,
            rate_category="standard" if product is ProductType.HOME_LOAN else "term_deposit",
            observed_at=when,
        )
        write = service.store.record_observations(
            [record],
            run_id="postcommit-timeout",
            observed_at=when,
            product_type=product,
        )
        before = service.store._connection.execute(
            "SELECT * FROM observations ORDER BY id"
        ).fetchall()
        persisted, note = record_failed_attempt(
            service.store,
            run_id="postcommit-timeout",
            product_type=product,
            started_at=when,
            banks_attempted=tuple(market.banks),
            error_summary="collection_timeout: stopped during scenario calculation",
        )
        assert persisted is True, "a committed observation must not suppress the failed attempt"
        failed = service.store.get_latest_run_record(product)
        assert failed.run_id == "postcommit-timeout" and failed.status == "FAILED"
        assert failed.collection_run_id == "postcommit-timeout"
        assert failed.rates_observed == write.total
        assert set(failed.banks_failed) == set(market.banks)
        assert service.store.get_latest_run_record(other) == other_before
        health = assess_system_health(
            service, product_type=product, now=when + timedelta(minutes=1)
        )
        assert run_health(health.latest_run) is RunHealth.FAILED
        assert health.confirmed_fresh_bank_count == 0
        assert all(bank.operational.state is OperationalState.DEGRADED for bank in health.banks)
        assert (
            service.store._connection.execute("SELECT * FROM observations ORDER BY id").fetchall()
            == before
        )
        again = record_failed_attempt(
            service.store,
            run_id="postcommit-timeout",
            product_type=product,
            started_at=when,
            banks_attempted=(),
            error_summary="retry must not replace evidence",
        )
        assert again[0] is False
        assert service.store.get_run_record("postcommit-timeout") == failed


def test_in_process_timeout_summary_keeps_previously_committed_counts(
    workflow, tmp_path, make_record
):
    path = tmp_path / "market.db"
    with ObservationStore(path) as store:
        store.record_observations([make_record()], run_id="late-timeout", observed_at=NOW)
        code, summary = workflow._timeout_failure(
            CollectionTimeoutError("scenario", 11, 10),
            store=store,
            product_type=ProductType.HOME_LOAN,
            summary_path=None,
            run_id="late-timeout",
            started_at=NOW,
            banks=("ANZ",),
        )
        assert code == 1
        assert summary["run_record_persisted"] is True
        assert summary["observations_committed"] is True
        assert summary["rates_accepted"] == 1 and summary["new_states_stored"] == 1
        assert store.get_run_record("late-timeout").collection_run_id == "late-timeout"


def test_supervisor_timeout_after_real_commit_records_failed_attempt(
    workflow, tmp_path, make_record, monkeypatch
):
    database = tmp_path / "market.db"
    summary_path = tmp_path / "summary.json"
    record_json = make_record(observed_at=NOW).model_dump_json()

    def command(argv, *, run_id, soft_deadline_seconds):
        script = textwrap.dedent(f"""
            import time
            from datetime import datetime
            from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord
            from balance_pricing_engine.pricing_intelligence.store import ObservationStore
            with ObservationStore({str(database)!r}) as store:
                record = CompetitorRateRecord.model_validate_json({record_json!r})
                store.record_observations([record], run_id={run_id!r},
                                          observed_at=datetime.fromisoformat({NOW.isoformat()!r}))
                time.sleep(120)
        """)
        return [PYTHON, "-c", script]

    monkeypatch.setattr(workflow, "_worker_command", command)
    code = workflow.main(
        [
            "--database",
            str(database),
            "--lock-file",
            str(tmp_path / "collect.lock"),
            "--summary-file",
            str(summary_path),
            "--quiet",
            "--run-id",
            "supervised-postcommit",
            "--run-timeout-seconds",
            "5",
        ]
    )
    assert code == 1
    summary = json.loads(summary_path.read_text())
    with ObservationStore.open_reader(database) as store:
        assert len(store.get_current_observations()) == 1
        failed = store.get_run_record("supervised-postcommit")
        assert failed is not None and failed.status == "FAILED"
        assert failed.rates_observed == 1
        assert failed.collection_run_id == "supervised-postcommit"
    assert summary["failure_type"] == "collection_timeout"
    assert summary["run_record_persisted"] is True
    assert summary["observations_committed"] is True and summary["rates_accepted"] == 1


@pytest.mark.parametrize("product", [ProductType.HOME_LOAN, ProductType.TERM_DEPOSIT])
def test_unexpected_postcommit_failure_keeps_counts_and_records_failed_attempt(
    workflow, tmp_path, make_record, monkeypatch, product
):
    path = tmp_path / "market.db"
    config = PricingIntelligenceConfig(database_path=str(path), selected_banks=["ANZ"])
    record = make_record(
        product_type=product,
        rate_category="standard" if product is ProductType.HOME_LOAN else "term_deposit",
        observed_at=NOW,
    )
    with ObservationStore(path) as store:
        store.record_observations([record], run_id="previous", observed_at=NOW)
    committed_rows = []

    def fail_after_commit(service, **kwargs):
        service.store.record_observations(
            [record], run_id=kwargs["run_id"], observed_at=NOW, product_type=product
        )
        committed_rows.extend(
            service.store._connection.execute("SELECT * FROM observations").fetchall()
        )
        raise RuntimeError("bookkeeping failed after commit")

    monkeypatch.setattr(workflow, "run_operational_cycle", fail_after_commit)
    code, summary = workflow.run_collection(
        config, False, product_type=product, run_id="unexpected-postcommit"
    )
    assert code == 1 and summary["failure_type"] == "unexpected_error"
    assert summary["run_record_persisted"] is True
    assert summary["observations_committed"] is True
    assert summary["rates_accepted"] == summary["unchanged_states"] == 1
    assert summary["new_states_stored"] == 0
    with ObservationStore.open_reader(path) as store:
        failed = store.get_run_record("unexpected-postcommit")
        assert failed is not None and failed.status == "FAILED"
        assert failed.product_type == product and failed.banks_failed == ("ANZ",)
        assert failed.rates_observed == 1
        assert failed.collection_run_id == "unexpected-postcommit"
        assert store._connection.execute("SELECT * FROM observations").fetchall() == committed_rows


def test_failed_attempt_checks_existing_record_under_sqlite_write_lock(tmp_path, monkeypatch):
    from balance_pricing_engine.pricing_intelligence.orchestration import record_failed_attempt

    path = tmp_path / "market.db"
    with ObservationStore(path) as store:
        original_get = store.get_run_record

        def check_locked(run_id):
            with sqlite3.connect(path, timeout=0) as other:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("BEGIN IMMEDIATE")
            return original_get(run_id)

        monkeypatch.setattr(store, "get_run_record", check_locked)
        persisted, _ = record_failed_attempt(
            store,
            run_id="exclusive-check",
            product_type=ProductType.HOME_LOAN,
            started_at=NOW,
            banks_attempted=("ANZ",),
            error_summary="worker_failed",
        )
        assert persisted is True
