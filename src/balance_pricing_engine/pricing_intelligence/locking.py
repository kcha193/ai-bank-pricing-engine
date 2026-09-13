"""Single-instance locking for pricing-intelligence operational runs.

Relocated from ``scripts/collect_market_rates_workflow.py`` (which originally
defined this for its own scheduled entry point) so both that script and the
interactive ``pricing_intelligence run`` CLI command share one implementation
rather than each taking their own advisory lock against potentially different
paths.
"""

from __future__ import annotations

import errno
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    """A collection is already in progress."""


class SingleInstanceLock:
    """Advisory file lock ensuring one collection runs at a time.

    A slow browser-rendered collection can overrun its schedule. Without this,
    the next invocation would start against the same store and both would write
    a run, producing duplicate observations and spurious changes.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> "SingleInstanceLock":
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise AlreadyRunningError(f"another collection holds {self.path}") from exc
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
