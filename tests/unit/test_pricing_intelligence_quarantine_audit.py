"""Quarantine is based on independent evidence, not parser repeatability."""

from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.competitor_rates.collectors import (
    BankCollectionResult,
    CollectionStatus,
)
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


class Collector:
    bank = "ANZ"

    def __init__(self, record):
        self.record = record

    def collect_mortgage_rates(self, captured_at_utc=None, persist_snapshot=False):
        return BankCollectionResult(
            bank=self.bank,
            product_type=ProductType.HOME_LOAN,
            status=CollectionStatus.SUCCESS,
            source_url=self.record.source_url,
            records=(self.record.model_copy(update={"captured_at_utc": captured_at_utc}),),
        )


def collect_sequence(make_record, rates):
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(
            collection=CollectionConfig(inter_request_delay_seconds=0)
        ),
        store=ObservationStore(":memory:"),
        collectors=[],
    )
    for index, rate in enumerate(rates):
        service._collectors = [Collector(make_record(rate_pct=rate))]
        service.collect(captured_at_utc=NOW + timedelta(hours=index), persist_snapshot=False)
    return service


def test_repeated_parser_spike_and_small_hops_remain_quarantined(make_record):
    with collect_sequence(make_record, [4.89, 8.49, 8.49, 8.59]) as service:
        assert service.resolve_competitor_assumption(now=NOW + timedelta(hours=3)).rate_pct == 4.89
        assert service.get_market_activity(now=NOW + timedelta(hours=3)).changes == 0
        assert service.get_market_activity(now=NOW + timedelta(hours=1)).changes == 0
        assert all(c.suspicious for c in service.get_changes(now=NOW + timedelta(hours=3)))


def test_clean_recovery_compares_with_last_valid_rate(make_record):
    with collect_sequence(make_record, [4.89, 8.49, 4.99]) as service:
        now = NOW + timedelta(hours=2)
        assert service.resolve_competitor_assumption(now=now).rate_pct == 4.99
        valid = [c for c in service.get_changes(now=now) if not c.suspicious]
        assert len(valid) == 1
        assert (valid[0].previous_rate_pct, valid[0].current_rate_pct, valid[0].change_bps) == (
            4.89,
            4.99,
            10.0,
        )


def test_backdated_collection_cannot_rewind_last_seen(make_record):
    with ObservationStore(":memory:") as store:
        later = NOW + timedelta(days=1)
        store.record_observations([make_record(observed_at=later)], observed_at=later)
        with pytest.raises(ValueError, match="chronological"):
            store.record_observations([make_record(observed_at=NOW)], observed_at=NOW)
        assert store.get_latest_rates()[0].captured_at_utc == later
