"""Shared fixtures for dashboard/health tests: a deterministic, controlled NZ market."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.changes import detect_changes
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.store import ObservationStore, RunRecord

NOW = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)
BANKS = ("ANZ", "ASB", "BNZ", "Westpac", "Kiwibank")
BASE_RATES = {"ANZ": 4.89, "ASB": 4.89, "BNZ": 4.85, "Westpac": 4.85, "Kiwibank": 4.89}


@pytest.fixture
def market_factory(tmp_path, make_record):
    """Build a 12-month mortgage market over three runs (NOW-10d, NOW-2d, NOW-1d).

    Run 2 (NOW-1d) moves ANZ 4.89 -> ``anz_new_rate``. Banks in ``stale_banks`` are
    only observed in the first run and then record failed collections, so their last
    observation is 10 days old (older than the 168 h default freshness window).
    ``anz_new_status="warning"`` stores ANZ's new reading as quarantined (suspicious), as the
    real validation step would for an implausible jump.
    """

    def build(
        *,
        anz_new_rate: float = 4.79,
        anz_new_status: str = "unvalidated",
        stale_banks=(),
        name: str = "market.db",
    ):
        database = tmp_path / name
        config = PricingIntelligenceConfig(database_path=str(database))
        store = ObservationStore(database)
        runs = (
            ("run-0", NOW - timedelta(days=10), dict(BASE_RATES)),
            ("run-1", NOW - timedelta(days=2), dict(BASE_RATES)),
            ("run-2", NOW - timedelta(days=1), {**BASE_RATES, "ANZ": anz_new_rate}),
        )
        previous = None
        for index, (run_id, when, rates) in enumerate(runs):
            live = {b: r for b, r in rates.items() if index == 0 or b not in stale_banks}
            records = [
                make_record(
                    bank=b,
                    rate_pct=r,
                    observed_at=when,
                    **(
                        {"validation_status": anz_new_status}
                        if (b == "ANZ" and run_id == "run-2")
                        else {}
                    ),
                )
                for b, r in live.items()
            ]
            store.record_observations(
                records, run_id=run_id, observed_at=when, product_type=ProductType.HOME_LOAN
            )
            if previous is not None:
                store.record_changes(
                    detect_changes(
                        previous,
                        records,
                        detected_at=when,
                        thresholds=config.validation,
                        run_id=run_id,
                        banks_collected=list(live),
                    )
                )
            for bank in BANKS:
                ok = bank in live
                store.update_collector_health(
                    bank,
                    ProductType.HOME_LOAN,
                    success=ok,
                    error=None if ok else "ConnectionError",
                    at=when,
                    run_id=run_id,
                )
            store.write_run_record(
                RunRecord(
                    run_id=f"op-{run_id}",
                    started_at=when,
                    completed_at=when + timedelta(minutes=2),
                    status="SUCCESS" if len(live) == len(BANKS) else "PARTIAL_SUCCESS",
                    product_type=ProductType.HOME_LOAN,
                    banks_attempted=BANKS,
                    banks_successful=tuple(live),
                    banks_failed=tuple(b for b in BANKS if b not in live),
                    rates_observed=len(live),
                    rates_changed=0,
                    validation_failures=0,
                    suspicious_observations=0,
                    market_state="NORMAL",
                    error_summary=None,
                    collection_run_id=run_id,
                )
            )
            previous = records
        store.close()
        return SimpleNamespace(config=config, database=database, now=NOW, banks=BANKS)

    return build


@pytest.fixture
def quarantine_market_factory(market_factory, make_record):
    """Independent mortgage/deposit quarantines with a trusted deposit baseline."""

    def build(mortgage, deposit):
        market = market_factory(
            anz_new_rate=8.49 if mortgage else 4.79,
            anz_new_status="warning" if mortgage else "unvalidated",
        )
        if deposit:
            store = ObservationStore(market.database)
            previous = [
                make_record(
                    bank="ASB",
                    product_type=ProductType.TERM_DEPOSIT,
                    product_name="Term Deposit",
                    rate_category="term_deposit",
                    rate_pct=4.0,
                    observed_at=NOW - timedelta(days=2),
                )
            ]
            current = [
                previous[0].model_copy(
                    update={
                        "advertised_rate_pct": 8.0,
                        "validation_status": "warning",
                        "captured_at_utc": NOW - timedelta(days=1),
                    }
                )
            ]
            for run_id, records in (("td-0", previous), ("td-1", current)):
                store.record_observations(
                    records,
                    run_id=run_id,
                    observed_at=records[0].captured_at_utc,
                    product_type=ProductType.TERM_DEPOSIT,
                )
            store.record_changes(
                detect_changes(
                    previous,
                    current,
                    detected_at=NOW - timedelta(days=1),
                    thresholds=market.config.validation,
                    run_id="td-1",
                    banks_collected=["ASB"],
                )
            )
            store.close()
        return market

    return build
