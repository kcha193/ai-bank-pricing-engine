"""Hard deadline for a collection worker process (DATA-008).

A cooperative deadline check (``competitor_rates.run_deadline``) cannot interrupt a hung
synchronous browser call. The scheduled workflow therefore runs each collection in a child
process and this module enforces the deadline from outside it:

1. The worker starts in its own session (process group), so it can be signalled as a unit.
2. On timeout the supervisor snapshots the worker's whole process tree *first*. Playwright
   launches Chromium detached (its own session), so a process-group signal alone would miss
   it, and once the driver exits its browsers are re-parented and no longer findable.
3. Everything in the snapshot gets SIGTERM (Playwright's driver closes its browsers on
   SIGTERM), then, after ``grace_seconds``, SIGKILL for anything still alive.
4. The worker is reaped, so no zombie remains and any inherited lock descriptor closes.

Portable across macOS and Linux: uses ``ps -A -o pid=,ppid=`` and POSIX signals only, never
GNU ``timeout`` (not installed on macOS by default). A SIGKILLed worker cannot commit
anything afterwards; SQLite rolls back a write transaction it left open.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import signal
import subprocess
import time
from typing import Callable, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_GRACE_SECONDS = 10.0


@dataclass(frozen=True)
class SupervisedRun:
    pid: int
    returncode: Optional[int]
    timed_out: bool
    elapsed_seconds: float
    #: Every process signalled after the timeout (the worker and its descendants).
    terminated_pids: tuple[int, ...] = ()
    #: True when anything ignored SIGTERM and had to be SIGKILLed.
    killed_after_grace: bool = False


def _process_table() -> dict[int, list[int]]:
    """Parent pid -> child pids, from ``ps``. Empty if ``ps`` is unavailable."""
    try:
        output = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not list processes (%s); falling back to a process-group kill", exc)
        return {}
    children: dict[int, list[int]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return children


def process_tree(root_pid: int) -> list[int]:
    """``root_pid`` and all its current descendants, root first."""
    children = _process_table()
    tree, frontier = [root_pid], [root_pid]
    while frontier:
        parent = frontier.pop()
        for child in children.get(parent, []):
            if child not in tree:
                tree.append(child)
                frontier.append(child)
    return tree


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def terminate_process_tree(
    process: subprocess.Popen, grace_seconds: float = DEFAULT_GRACE_SECONDS
) -> tuple[tuple[int, ...], bool]:
    """SIGTERM then SIGKILL ``process`` and every descendant; reap ``process``.

    Returns ``(signalled_pids, killed_after_grace)``.
    """
    tree = process_tree(process.pid)
    for pid in tree:
        _signal(pid, signal.SIGTERM)
    _signal_group(process.pid, signal.SIGTERM)

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        process.poll()
        if not any(_alive(pid) for pid in tree if pid != process.pid) and process.returncode is not None:
            break
        time.sleep(0.05)

    killed = False
    survivors = [pid for pid in tree if _alive(pid)]
    if process.poll() is None or [pid for pid in survivors if pid != process.pid]:
        killed = True
        _signal_group(process.pid, signal.SIGKILL)
        for pid in survivors:
            _signal(pid, signal.SIGKILL)
    process.wait()
    return tuple(tree), killed


def run_supervised(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    pass_fds: Sequence[int] = (),
    clock: Callable[[], float] = time.monotonic,
) -> SupervisedRun:
    """Run ``command`` and terminate its whole process tree at ``timeout_seconds``.

    ``pass_fds`` lets the worker inherit the caller's single-instance lock descriptor, so
    the advisory lock stays held while either process is alive -- if the supervisor itself
    is killed, a still-running worker keeps the lock and later runs skip (and escalate)
    rather than overlap it. Any exception in the supervisor (including SIGTERM converted to
    ``SystemExit`` by the caller) still terminates the worker tree before propagating.
    """
    started = clock()
    process = subprocess.Popen(list(command), pass_fds=tuple(pass_fds), start_new_session=True)
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        terminated, killed = terminate_process_tree(process, grace_seconds)
        logger.error(
            "collection worker %s exceeded %.0fs; terminated %d process(es)%s",
            process.pid,
            timeout_seconds,
            len(terminated),
            " (SIGKILL after grace)" if killed else "",
        )
        return SupervisedRun(
            pid=process.pid,
            returncode=process.returncode,
            timed_out=True,
            elapsed_seconds=clock() - started,
            terminated_pids=terminated,
            killed_after_grace=killed,
        )
    except BaseException:
        if process.poll() is None:
            terminate_process_tree(process, grace_seconds)
        raise
    return SupervisedRun(
        pid=process.pid,
        returncode=returncode,
        timed_out=False,
        elapsed_seconds=clock() - started,
    )
