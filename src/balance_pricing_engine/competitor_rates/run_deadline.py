"""End-to-end deadline for one product-scoped collection run (DATA-008).

``request_timeout_seconds`` bounds one request; retries, backoff, browser start-up and
five banks multiply it. :class:`RunDeadline` bounds the whole run on the monotonic clock
(immune to wall-clock steps) and is checked at the safe points the collection controls:
before each bank and before anything is persisted.

This is the *cooperative* half of the guarantee: a check only runs between blocking calls,
so it cannot interrupt a genuinely hung browser call. The scheduled workflow therefore also
runs collection in a supervised child process that is terminated at a hard deadline (see
``pricing_intelligence.supervisor``). Lives in ``competitor_rates`` so the lower collector
layer can honour it without importing ``pricing_intelligence``.
"""

from __future__ import annotations

import math
import time
from typing import Callable

#: Stable ``failure_type`` / ``error_type`` for a run stopped by its deadline.
COLLECTION_TIMEOUT = "collection_timeout"


class CollectionTimeoutError(RuntimeError):
    """The run exceeded its end-to-end deadline; nothing further was fetched or written."""

    def __init__(self, stage: str, elapsed_seconds: float, timeout_seconds: float) -> None:
        super().__init__(
            f"collection exceeded its {timeout_seconds:g}s deadline at {stage} "
            f"(elapsed {elapsed_seconds:.1f}s)"
        )
        self.stage = stage
        self.elapsed_seconds = elapsed_seconds
        self.timeout_seconds = timeout_seconds


class RunDeadline:
    """A monotonic-clock budget started at construction."""

    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        if not (math.isfinite(seconds) and seconds > 0):
            raise ValueError(f"deadline must be a positive finite number of seconds, got {seconds!r}")
        self.timeout_seconds = float(seconds)
        self._clock = clock
        self._started = clock()

    @property
    def elapsed(self) -> float:
        return self._clock() - self._started

    @property
    def remaining(self) -> float:
        return self.timeout_seconds - self.elapsed

    @property
    def expired(self) -> bool:
        return self.remaining <= 0

    def check(self, stage: str) -> None:
        """Raise :class:`CollectionTimeoutError` if the budget is spent."""
        if self.expired:
            raise CollectionTimeoutError(stage, self.elapsed, self.timeout_seconds)
