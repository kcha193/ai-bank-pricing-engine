"""Verify a loopback listener using OS process ownership before opening Pricing Watch.

Invoked by file path so the launcher needs only the standard library for this probe.
Unknown ownership, ambiguous listeners and unavailable process tools fail closed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from typing import Optional, Sequence
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


def _process_identity(
    port: int, command: Sequence[str], parent_pid: Optional[int]
) -> Optional[str]:
    def read(args: list[str]) -> str:
        return subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()

    try:
        listeners = read(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"])
        pids = {line[1:] for line in listeners.splitlines() if line.startswith("p")}
        if len(pids) != 1:
            return None
        pid = pids.pop()
        if not pid.isdecimal():
            return None
        if read(["ps", "-ww", "-p", pid, "-o", "uid="]) != str(os.getuid()):
            return None
        actual_command = read(["ps", "-ww", "-p", pid, "-o", "command="])
        # macOS framework Python execs Python.app, so ps may show that executable
        # instead of the virtualenv symlink used to launch it. Verify the same
        # local interpreter's OS name while keeping every Streamlit argument exact.
        runtime_executable = read(["ps", "-ww", "-p", str(os.getpid()), "-o", "comm="])
        expected_commands = {" ".join(command)}
        if os.path.isabs(runtime_executable):
            expected_commands.add(" ".join([runtime_executable, *command[1:]]))
        if actual_command not in expected_commands:
            return None
        if parent_pid is not None and read(["ps", "-ww", "-p", pid, "-o", "ppid="]) != str(
            parent_pid
        ):
            return None
        started = read(["ps", "-ww", "-p", pid, "-o", "lstart="])
        if not started:
            return None
        return hashlib.sha256(f"{pid}|{started}".encode()).hexdigest()
    except (OSError, subprocess.SubprocessError):
        return None


def _healthy(port: int) -> bool:
    try:
        # Loopback health must never be routed through a configured HTTP proxy.
        with build_opener(ProxyHandler({})).open(
            f"http://127.0.0.1:{port}/_stcore/health", timeout=2
        ) as response:
            return response.status == 200 and response.read(3) == b"ok"
    except (OSError, URLError):
        return False


def verified_listener(
    port: int,
    command: Sequence[str],
    *,
    parent_pid: Optional[int] = None,
    expected_identity: Optional[str] = None,
) -> Optional[str]:
    """Return a stable PID/start-time token only for the expected healthy local process.

    Ownership is checked on both sides of health, and again by the launcher immediately
    before opening. During startup, require the listener to be the launcher's own child.
    """
    before = _process_identity(port, command, parent_pid)
    if before is None or (expected_identity is not None and before != expected_identity):
        return None
    if not _healthy(port):
        return None
    after = _process_identity(port, command, parent_pid)
    return before if before == after else None


def main() -> int:
    port, app, python = sys.argv[1:4]
    parent = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None
    expected = (sys.argv[5] or None) if len(sys.argv) > 5 else None
    command = [
        python,
        "-m",
        "streamlit",
        "run",
        app,
        "--server.address",
        "127.0.0.1",
        "--server.port",
        port,
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    token = verified_listener(int(port), command, parent_pid=parent, expected_identity=expected)
    if token is None:
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
