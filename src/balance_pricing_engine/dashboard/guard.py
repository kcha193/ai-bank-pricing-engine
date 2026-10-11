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
    """Allows ``max_failures`` guesses, then locks for ``lockout_seconds``.

    ``try_acquire`` *charges* the attempt before the caller compares anything, atomically, so a
    burst of parallel guesses cannot all slip past a check-then-record gap. A successful guess
    calls ``record_success`` to clear the count.
    """

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
        self._attempts = 0
        self._locked_until: Optional[float] = None
        self._lock = threading.Lock()

    def _locked_for_locked(self) -> float:
        if self._locked_until is None:
            return 0.0
        remaining = self._locked_until - self._monotonic()
        if remaining <= 0:  # lockout over: start again with a full allowance
            self._locked_until = None
            self._attempts = 0
            return 0.0
        return remaining

    def locked_for(self) -> float:
        """Seconds of lockout remaining (0 when attempts are allowed)."""
        with self._lock:
            return self._locked_for_locked()

    def try_acquire(self) -> float:
        """Charge one attempt. Returns 0 if it may proceed, else the lockout seconds remaining."""
        with self._lock:
            remaining = self._locked_for_locked()
            if remaining > 0:
                return remaining
            self._attempts += 1
            if self._attempts >= self._max_failures:
                self._locked_until = self._monotonic() + self._lockout_seconds
            return 0.0

    def record_success(self) -> None:
        with self._lock:
            self._attempts = 0
            self._locked_until = None


_UNLOCK_ATTEMPTS = AttemptLimiter()


def unlock_attempts() -> AttemptLimiter:
    """The one limiter shared by every session in this server process."""
    return _UNLOCK_ATTEMPTS
