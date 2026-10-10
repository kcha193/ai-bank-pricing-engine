"""Process-wide brute-force protection for the Operations unlock.

The failure count must not live in a browser session: opening a new tab or reloading would
hand an attacker a fresh set of guesses. One limiter is shared by every session in the server
process. The trade-off is deliberate: while it is locked, *nobody* can unlock (including the
operator) until the lockout ends, which is far better than an unbounded guessing oracle.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

MAX_FAILURES = 5
LOCKOUT_SECONDS = 300.0


class AttemptLimiter:
    """Locks for ``lockout_seconds`` after ``max_failures`` consecutive failures."""

    def __init__(
        self,
        max_failures: int = MAX_FAILURES,
        lockout_seconds: float = LOCKOUT_SECONDS,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._lockout_seconds = lockout_seconds
        self._monotonic = monotonic
        self._failures = 0
        self._locked_until: Optional[float] = None
        self._lock = threading.Lock()

    def locked_for(self) -> float:
        """Seconds of lockout remaining (0 when attempts are allowed)."""
        with self._lock:
            return self._locked_for_locked()

    def _locked_for_locked(self) -> float:
        if self._locked_until is None:
            return 0.0
        remaining = self._locked_until - self._monotonic()
        if remaining <= 0:  # lockout over: start again with a full allowance
            self._locked_until = None
            self._failures = 0
            return 0.0
        return remaining

    def record_failure(self) -> None:
        with self._lock:
            if self._locked_for_locked() > 0:
                return  # already locked: further failures do not extend it
            self._failures += 1
            if self._failures >= self._max_failures:
                self._locked_until = self._monotonic() + self._lockout_seconds

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._locked_until = None


_UNLOCK_ATTEMPTS = AttemptLimiter()


def unlock_attempts() -> AttemptLimiter:
    """The one limiter shared by every session in this server process."""
    return _UNLOCK_ATTEMPTS
