"""Guard against tests that expire as real time passes.

On 2026-08-27 seven CLI tests began failing without a code change. They pinned
their collection data to a fixed calendar date, while market snapshots exclude
observations older than ``max_observation_age_hours`` (168h) measured against
the wall clock. The tests passed when written, passed in CI at merge, and
started failing once real time crossed the window.

Test data that feeds a wall-clock-relative code path must therefore be dated
relative to now.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

import pytest

from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig

#: Test modules whose collection timestamps feed a staleness-checked path.
CLOCK_SENSITIVE_MODULES = (
    "tests/unit/test_pricing_intelligence_cli.py",
    "tests/unit/test_dashboard_market_intelligence.py",
    "tests/unit/test_pricing_intelligence_service.py",
    "tests/unit/test_scheduled_collection_workflow.py",
)

# Matches any fixed-calendar-date `datetime(YYYY, ...)` construction, not just
# ones assigned to a variable literally named DAY_ONE/DAY_TWO -- a differently
# named or renamed anchor (DAY_THREE, NOW, ...) is exactly as capable of
# recreating the expiry bug this guard exists to catch.
_FIXED_DATE_ANCHOR = re.compile(r"datetime\(\d{4},")

# Matches the base "relative to now" anchors themselves (e.g. DAY_ONE/DAY_TWO),
# so the staleness-window check below is derived from what the fixtures
# actually use rather than a hand-maintained constant that can drift out of
# sync with them.
_RELATIVE_ANCHOR_OFFSET = re.compile(
    r"datetime\.now\(timezone\.utc\)\s*-\s*timedelta\((hours|days)=(\d+)\)"
)


@pytest.mark.parametrize("module_path", CLOCK_SENSITIVE_MODULES)
def test_collection_anchors_are_relative_to_now(module_path):
    """A fixed calendar anchor here is a test that expires."""
    source = Path(module_path).read_text(encoding="utf-8")
    offending_lines = [
        f"line {lineno}: {line.strip()}"
        for lineno, line in enumerate(source.splitlines(), start=1)
        if _FIXED_DATE_ANCHOR.search(line)
    ]

    assert offending_lines == [], (
        f"{module_path} pins a fixed calendar date at {offending_lines}. Market "
        "snapshots drop observations older than max_observation_age_hours, so "
        "this test will start failing once real time crosses that window. Use "
        "datetime.now(timezone.utc) - timedelta(...) instead."
    )


def test_the_staleness_window_is_wide_enough_for_relative_anchors():
    """The relative offsets used by those modules must sit inside the window."""
    window_hours = PricingIntelligenceConfig().max_observation_age_hours

    widest_offset_hours = 0
    for module_path in CLOCK_SENSITIVE_MODULES:
        source = Path(module_path).read_text(encoding="utf-8")
        for unit, amount in _RELATIVE_ANCHOR_OFFSET.findall(source):
            hours = int(amount) * (24 if unit == "days" else 1)
            widest_offset_hours = max(widest_offset_hours, hours)

    assert widest_offset_hours > 0, "expected at least one relative anchor to check"
    assert widest_offset_hours < window_hours, (
        f"a relative anchor uses a {widest_offset_hours}h offset, which is outside "
        f"the {window_hours}h staleness window -- that anchor's observations would "
        "already be dropped as stale, recreating the expiry bug this guard exists "
        "to catch."
    )


def test_a_fixed_anchor_would_actually_expire():
    """Documents the failure mode, so the guard above is not cargo-culted.

    An observation dated one hour beyond the window is excluded; the same
    observation dated relative to now is not.
    """
    window_hours = PricingIntelligenceConfig().max_observation_age_hours
    now = datetime.now(timezone.utc)

    expired = now - timedelta(hours=window_hours + 1)
    fresh = now - timedelta(hours=2)

    assert (now - expired) > timedelta(hours=window_hours)
    assert (now - fresh) < timedelta(hours=window_hours)
