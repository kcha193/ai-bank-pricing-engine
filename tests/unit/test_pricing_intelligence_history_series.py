"""``get_rate_history_series`` buckets by calendar day; the zone is the caller's choice.

Kept apart from ``test_pricing_intelligence_service.py``: that module is guarded against fixed
calendar dates because its data feeds the wall-clock staleness check. History is not
staleness-filtered, and a fixed instant is the point of this test (a UTC/NZ day boundary).
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

NZ = ZoneInfo("Pacific/Auckland")
# 09:00 NZDT on 12 Oct is 20:00 UTC on 11 Oct.
NZ_MORNING_REPRICE = datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc)


def _service(tmp_path, make_record):
    config = PricingIntelligenceConfig(database_path=str(tmp_path / "m.db"))
    store = ObservationStore(config.database_path)
    store.record_observations(
        [make_record(bank="ANZ", rate_pct=4.79, observed_at=NZ_MORNING_REPRICE)],
        observed_at=NZ_MORNING_REPRICE,
    )
    return PricingIntelligenceService(config=config, store=store)


def test_the_default_zone_is_utc(tmp_path, make_record):
    with _service(tmp_path, make_record) as service:
        days = [day for day, _ in service.get_rate_history_series(term_months=12)]
    assert days == [date(2026, 10, 11)]


def test_a_local_zone_buckets_a_morning_repricing_under_the_local_day(tmp_path, make_record):
    with _service(tmp_path, make_record) as service:
        series = service.get_rate_history_series(term_months=12, tz=NZ)
    assert [day for day, _ in series] == [date(2026, 10, 12)]
    assert series[0][1] == {"ANZ": 4.79}
