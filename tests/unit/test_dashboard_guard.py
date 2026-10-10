from balance_pricing_engine.dashboard.guard import AttemptLimiter


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_the_limiter_locks_after_the_maximum_failures():
    clock = FakeClock()
    limiter = AttemptLimiter(max_failures=3, lockout_seconds=300, monotonic=clock)
    for _ in range(2):
        limiter.record_failure()
        assert limiter.locked_for() == 0
    limiter.record_failure()
    assert limiter.locked_for() == 300


def test_the_lockout_counts_down_and_then_clears_with_a_fresh_allowance():
    clock = FakeClock()
    limiter = AttemptLimiter(max_failures=2, lockout_seconds=60, monotonic=clock)
    limiter.record_failure()
    limiter.record_failure()
    clock.now += 45
    assert limiter.locked_for() == 15
    clock.now += 16
    assert limiter.locked_for() == 0
    limiter.record_failure()  # the allowance restarted: one failure does not re-lock
    assert limiter.locked_for() == 0


def test_a_success_clears_earlier_failures():
    limiter = AttemptLimiter(max_failures=3, lockout_seconds=60, monotonic=FakeClock())
    limiter.record_failure()
    limiter.record_failure()
    limiter.record_success()
    limiter.record_failure()
    limiter.record_failure()
    assert limiter.locked_for() == 0


def test_failures_while_locked_do_not_extend_the_lockout():
    clock = FakeClock()
    limiter = AttemptLimiter(max_failures=1, lockout_seconds=60, monotonic=clock)
    limiter.record_failure()
    clock.now += 30
    limiter.record_failure()
    assert limiter.locked_for() == 30
