"""Operational actions. No Streamlit imports; nothing here is reachable from a read path.

Live collection runs as a subprocess of the *scheduled* workflow script so it shares that
script's single-instance lock, exit codes, scenario trigger and JSON summary, and so Playwright
never runs inside Streamlit's script thread. Only ``pages/operations.py`` may import this module
(enforced by ``tests/unit/test_dashboard_boundaries.py``).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from balance_pricing_engine.dashboard.settings import DashboardSettings
from balance_pricing_engine.pricing_intelligence.alerts import (
    AlertSink,
    build_telegram_sink_from_env,
    plain_alert,
)
from balance_pricing_engine.pricing_intelligence.backup import backup_database

logger = logging.getLogger(__name__)

WORKFLOW_SCRIPT = Path("scripts") / "collect_market_rates_workflow.py"
WORKER_MODULE = "balance_pricing_engine.dashboard.run_worker"
DEFAULT_TIMEOUT_SECONDS = 1800

# Exit codes of the scheduled workflow (orchestration.EXIT_*): 0 ok, 1 total failure,
# 2 partial failure, 3 already running. Anything else is a failure.
_STATUS_BY_EXIT = {0: "SUCCESS", 1: "FAILED", 2: "PARTIAL_SUCCESS", 3: "ALREADY_RUNNING"}
_MESSAGES = {
    "SUCCESS": "Pricing Watch run completed.",
    "PARTIAL_SUCCESS": (
        "Run finished but some banks failed; their last rates are marked stale. See System Health."
    ),
    "FAILED": "Run failed; stored data is unchanged. See System Health and the application log.",
    "ALREADY_RUNNING": "Another Pricing Watch run is already in progress; nothing was started.",
}
_DRY_RUN_NOTE = (
    " Dry run: nothing was persisted, no alerts were sent, collector health was not updated."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BankOutcome:
    bank: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class ActionResult:
    action: str
    status: str  # SUCCESS | PARTIAL_SUCCESS | FAILED | ALREADY_RUNNING | NOT_CONFIGURED | TIMED_OUT
    message: str
    completed_at: datetime
    elapsed_seconds: float = 0.0
    banks: tuple[BankOutcome, ...] = ()
    summary: Mapping[str, object] = field(default_factory=dict)


def _subprocess_env(settings: DashboardSettings) -> dict[str, str]:
    env = dict(os.environ)
    src = str(settings.repo_root / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _banks_from_summary(summary: Mapping[str, Any]) -> tuple[BankOutcome, ...]:
    """Per-bank results from a ``build_summary`` dict (parsed JSON, so shapes are not trusted)."""
    failures = {
        f.get("bank"): str(f.get("error_type", ""))
        for f in summary.get("failures") or []
        if isinstance(f, dict)
    }
    succeeded = [BankOutcome(str(b), True) for b in summary.get("banks_succeeded") or []]
    failed = [
        BankOutcome(str(b), False, failures.get(b, "")) for b in summary.get("banks_failed") or []
    ]
    return tuple(succeeded + failed)


def _read_json(path: Path) -> Optional[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _run_process(
    action: str,
    command: list[str],
    result_path: Path,
    settings: DashboardSettings,
    runner: Callable[..., subprocess.CompletedProcess],
    clock: Callable[[], datetime],
    timeout_seconds: float,
    *,
    summary_key: Optional[str],
    dry: bool,
) -> ActionResult:
    started = time.monotonic()
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(settings.repo_root),
            env=_subprocess_env(settings),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ActionResult(
            action,
            "TIMED_OUT",
            "The run exceeded its time limit and was stopped.",
            clock(),
            time.monotonic() - started,
        )
    except OSError:
        logger.exception("could not launch %s", action)
        return ActionResult(action, "FAILED", "The run could not be started; see the log.", clock())

    elapsed = time.monotonic() - started
    status = _STATUS_BY_EXIT.get(completed.returncode, "FAILED")
    payload = _read_json(result_path)
    summary = (payload.get(summary_key) if payload and summary_key else payload) or {}
    if dry and payload and payload.get("status") in _MESSAGES:
        status = str(payload["status"])
    message = _MESSAGES[status]
    if not summary and status != "ALREADY_RUNNING":
        message += " (Run summary could not be read.)"
    if dry:
        message += _DRY_RUN_NOTE
    return ActionResult(
        action, status, message, clock(), elapsed, _banks_from_summary(summary), dict(summary)
    )


def run_live(
    settings: DashboardSettings,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    clock: Callable[[], datetime] = _utcnow,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ActionResult:
    """Collect live rates by running the scheduled workflow script (one process, one lock)."""
    with tempfile.TemporaryDirectory() as tmp:
        result_path = Path(tmp) / "summary.json"
        command = [sys.executable, str(settings.repo_root / WORKFLOW_SCRIPT)]
        if settings.engine_config_path.exists():
            command += ["--config", str(settings.engine_config_path)]
        command += [
            "--database",
            settings.intelligence.database_path,
            "--render-javascript",
            "--quiet",
            "--summary-file",
            str(result_path),
        ]
        return _run_process(
            "live", command, result_path, settings, runner, clock, timeout_seconds,
            summary_key=None, dry=False,
        )  # fmt: skip


def run_dry(
    settings: DashboardSettings,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    clock: Callable[[], datetime] = _utcnow,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ActionResult:
    """One non-persisting operational cycle (see ``run_worker``): live pages, nothing stored."""
    with tempfile.TemporaryDirectory() as tmp:
        result_path = Path(tmp) / "result.json"
        command = [
            sys.executable,
            "-m",
            WORKER_MODULE,
            "--config",
            str(settings.engine_config_path),
            "--database",
            settings.intelligence.database_path,
            "--render-javascript",
            "--result-file",
            str(result_path),
        ]
        return _run_process(
            "dry_run", command, result_path, settings, runner, clock, timeout_seconds,
            summary_key="summary", dry=True,
        )  # fmt: skip


def create_backup(
    settings: DashboardSettings, *, clock: Callable[[], datetime] = _utcnow
) -> ActionResult:
    database = Path(settings.intelligence.database_path)
    if not database.exists():
        return ActionResult("backup", "FAILED", "No database to back up yet.", clock())
    try:
        path = backup_database(database, settings.backup_dir, now=clock())
    except Exception:  # noqa: BLE001 - one operational sentence; detail goes to the log
        logger.exception("backup failed")
        return ActionResult("backup", "FAILED", "Backup failed; see the log.", clock())
    return ActionResult(
        "backup", "SUCCESS", "Backup created.", clock(), summary={"path": str(path)}
    )


def send_test_notification(
    *,
    sink_factory: Callable[[], Optional[AlertSink]] = build_telegram_sink_from_env,
    clock: Callable[[], datetime] = _utcnow,
) -> ActionResult:
    sink = sink_factory()
    if sink is None:
        return ActionResult(
            "telegram_test",
            "NOT_CONFIGURED",
            "Telegram is not configured for this environment.",
            clock(),
        )
    try:
        sink.send(plain_alert("NZ Pricing Intelligence Telegram test successful.", ""))
    except Exception as exc:  # noqa: BLE001
        # Class name only, and no traceback: the exception text can embed the request URL,
        # which contains the bot token.
        logger.error("telegram test failed: %s", type(exc).__name__)
        return ActionResult(
            "telegram_test",
            "FAILED",
            "Telegram test failed; check the bot and chat settings.",
            clock(),
        )
    return ActionResult("telegram_test", "SUCCESS", "Test notification sent.", clock())


@dataclass(frozen=True)
class JobState:
    name: Optional[str]
    running: bool
    result: Optional[ActionResult]


class JobRunner:
    """Single-flight background runner: at most one long job per server process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._name: Optional[str] = None
        self._result: Optional[ActionResult] = None

    def submit(self, name: str, fn: Callable[[], ActionResult]) -> bool:
        """Start ``fn`` in the background; ``False`` (and nothing started) if one is running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._name, self._result = name, None
            self._thread = threading.Thread(target=self._run, args=(name, fn), daemon=True)
            self._thread.start()
            return True

    def _run(self, name: str, fn: Callable[[], ActionResult]) -> None:
        try:
            result = fn()
        except Exception:  # noqa: BLE001
            logger.exception("background job %s crashed", name)
            result = ActionResult(name, "FAILED", "The job crashed; see the log.", _utcnow())
        with self._lock:
            self._result = result

    def state(self) -> JobState:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return JobState(self._name, running, self._result)

    def wait(self, timeout: float = 5.0) -> None:
        """Block until the current job ends (tests)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def clear_result(self) -> None:
        with self._lock:
            self._result = None


_DEFAULT_RUNNER = JobRunner()


def default_runner() -> JobRunner:
    """The process-wide runner, so every browser tab shares one single-flight guard."""
    return _DEFAULT_RUNNER
