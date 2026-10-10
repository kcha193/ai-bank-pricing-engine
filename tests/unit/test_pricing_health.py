from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.pricing_intelligence.health import (
    CollectorStatus,
    FreshnessState,
    HealthLevel,
    assess_observation_freshness,
    assess_system_health,
    collector_status,
    is_stale,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import CollectorHealth, ObservationStore

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def _health(failures=0, success_age_h=1.0):
    return CollectorHealth(
        bank="ANZ",
        product_type="home_loan",
        consecutive_failures=failures,
        last_success_at=NOW - timedelta(hours=success_age_h),
        last_failure_at=None,
        last_error=None,
        last_alert_sent_at=None,
    )


def test_is_stale_is_strictly_older_than_the_window():
    assert not is_stale(NOW - timedelta(hours=168), NOW, 168)
    assert is_stale(NOW - timedelta(hours=168, seconds=1), NOW, 168)


@pytest.mark.parametrize(
    "entry,max_age,expected",
    [
        (None, 168, CollectorStatus.UNKNOWN),
        (_health(failures=2), 168, CollectorStatus.FAILING),
        (_health(success_age_h=200), 168, CollectorStatus.STALE),
        (_health(success_age_h=200), None, CollectorStatus.FRESH),
        (_health(), 168, CollectorStatus.FRESH),
    ],
)
def test_collector_status(entry, max_age, expected):
    assert collector_status(entry, NOW, max_age) is expected


def test_observation_freshness_states(make_record):
    fresh = [make_record(bank="ANZ", observed_at=NOW - timedelta(hours=5))]
    stale = [make_record(bank="ANZ", observed_at=NOW - timedelta(hours=400))]
    kwargs = dict(now=NOW, max_age_hours=168, run_ids=["r1"], latest_run_id="r1")
    assert assess_observation_freshness(fresh, "ANZ", **kwargs).state is FreshnessState.FRESH
    assert assess_observation_freshness(stale, "ANZ", **kwargs).state is FreshnessState.STALE
    missing = assess_observation_freshness([], "ANZ", **kwargs)
    assert missing.state is FreshnessState.UNKNOWN and missing.label == "Missing"


def test_observation_freshness_flags_a_bank_not_touched_by_the_latest_run(make_record):
    records = [make_record(bank="ANZ", observed_at=NOW - timedelta(hours=5))]
    result = assess_observation_freshness(
        records, "ANZ", now=NOW, max_age_hours=168, run_ids=["old-run"], latest_run_id="new-run"
    )
    assert result.not_fully_confirmed
    assert result.label == "Fresh; not fully confirmed in latest stored run"


def test_observation_freshness_quarantine_wins(make_record):
    records = [make_record(bank="ANZ", observed_at=NOW, validation_status="warning")]
    result = assess_observation_freshness(
        records, "ANZ", now=NOW, max_age_hours=168, run_ids=["r"], latest_run_id="r"
    )
    assert result.quarantined and result.label == "Quarantined readings"


def test_system_health_for_a_healthy_market(market_factory):
    market = market_factory()
    with PricingIntelligenceService(
        config=market.config, store=ObservationStore(market.database)
    ) as service:
        health = assess_system_health(service, now=market.now)
    assert health.database_ok
    assert health.snapshot_valid
    assert health.fresh_bank_count == 5
    assert health.latest_run.status == "SUCCESS"
    assert all(b.health is HealthLevel.HEALTHY for b in health.banks)


def test_system_health_marks_a_failed_bank_stale_and_degraded(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    with PricingIntelligenceService(
        config=market.config, store=ObservationStore(market.database)
    ) as service:
        health = assess_system_health(service, now=market.now)
    westpac = next(b for b in health.banks if b.bank == "Westpac")
    assert westpac.freshness.state is FreshnessState.STALE
    assert westpac.health is HealthLevel.DEGRADED
    assert westpac.consecutive_failures == 2
    assert health.fresh_bank_count == 4


def test_market_exclusions_explain_why_a_bank_dropped_out(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    with PricingIntelligenceService(
        config=market.config, store=ObservationStore(market.database)
    ) as service:
        exclusions = service.get_market_exclusions(now=market.now)
        snapshot = service.get_market_snapshot(now=market.now)
    assert [e.bank for e in exclusions] == ["Westpac"]
    assert exclusions[0].term_months == 12 and exclusions[0].threshold_hours == 168.0
    assert exclusions[0].age_hours == pytest.approx(240.0)
    assert "Westpac" not in snapshot.banks
