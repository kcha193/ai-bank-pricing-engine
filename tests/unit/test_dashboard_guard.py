import threading

from balance_pricing_engine.dashboard.guard import AttemptLimiter


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_the_limiter_allows_the_maximum_attempts_then_locks():
    limiter = AttemptLimiter(max_failures=3, lockout_seconds=300, monotonic=FakeClock())
    assert [limiter.try_acquire() for _ in range(3)] == [0, 0, 0]
    assert limiter.try_acquire() == 300  # the fourth guess is refused before any comparison
    assert limiter.locked_for() == 300


def test_the_lockout_counts_down_and_then_restarts_with_a_full_allowance():
    clock = FakeClock()
    limiter = AttemptLimiter(max_failures=2, lockout_seconds=60, monotonic=clock)
    limiter.try_acquire()
    limiter.try_acquire()
    clock.now += 45
    assert limiter.try_acquire() == 15
    clock.now += 16
    assert limiter.try_acquire() == 0  # lockout over: allowed again
    assert limiter.try_acquire() == 0  # and a fresh allowance of two
    assert limiter.try_acquire() > 0


def test_a_success_clears_earlier_attempts():
    limiter = AttemptLimiter(max_failures=3, lockout_seconds=60, monotonic=FakeClock())
    limiter.try_acquire()
    limiter.try_acquire()
    limiter.record_success()
    assert [limiter.try_acquire() for _ in range(3)] == [0, 0, 0]


def test_refused_attempts_do_not_extend_the_lockout():
    clock = FakeClock()
    limiter = AttemptLimiter(max_failures=1, lockout_seconds=60, monotonic=clock)
    limiter.try_acquire()
    clock.now += 30
    assert limiter.try_acquire() == 30
    assert limiter.locked_for() == 30


def test_parallel_guesses_cannot_exceed_the_cap():
    """The cap is charged atomically *before* the comparison, so a burst cannot race it."""
    limiter = AttemptLimiter(max_failures=5, lockout_seconds=300, monotonic=FakeClock())
    barrier = threading.Barrier(50)
    allowed = []

    def guess():
        barrier.wait()
        if limiter.try_acquire() == 0:
            allowed.append(1)

    threads = [threading.Thread(target=guess) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(allowed) == 5
