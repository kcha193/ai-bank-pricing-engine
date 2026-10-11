from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
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


# Ported from the retired whole-script dashboard tests: these two guard real defects in
# how "not fully confirmed" is decided, so they now run against the shared health model.
DAY_ONE = datetime.now(timezone.utc) - timedelta(days=2)
DAY_TWO = datetime.now(timezone.utc) - timedelta(days=1)


def _service(tmp_path):
    from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig

    path = tmp_path / "observations.db"
    config = PricingIntelligenceConfig(database_path=str(path))
    return PricingIntelligenceService(config=config, store=ObservationStore(path)), config


def test_freshness_is_not_skewed_by_a_newer_term_deposit_run(tmp_path, make_record):
    """A newer term-deposit run must not make mortgage rows look unconfirmed: an
    unscoped latest_run_id() would resolve to the deposit run."""
    service, config = _service(tmp_path)
    with service:
        service.store.record_observations(
            [make_record(bank=b, rate_pct=4.79) for b in config.selected_banks],
            observed_at=DAY_ONE,
        )
        service.store.record_observations(
            [
                make_record(
                    bank="ANZ",
                    product_type="term_deposit",
                    product_name="12 Month Term Deposit",
                    rate_category="standard",
                    rate_pct=4.5,
                )
            ],
            observed_at=DAY_TWO,
        )
        health = assess_system_health(service)
    assert [b.bank for b in health.banks if b.freshness.not_fully_confirmed] == []


def test_freshness_flags_a_bank_untouched_by_a_same_timestamp_later_run(tmp_path, make_record):
    """Two runs can share a started_at; "not fully confirmed" must use run identity."""
    service, config = _service(tmp_path)
    with service:
        service.store.record_observations(
            [make_record(bank=b, rate_pct=4.79) for b in config.selected_banks],
            observed_at=DAY_ONE,
            run_id="run-1",
        )
        service.store.record_observations(
            [make_record(bank=b, rate_pct=4.79) for b in config.selected_banks if b != "Kiwibank"],
            observed_at=DAY_ONE,
            run_id="run-2",
        )
        health = assess_system_health(service)
    assert [b.bank for b in health.banks if b.freshness.not_fully_confirmed] == ["Kiwibank"]


# --- exclusions and health must use the snapshot's exact record set (review finding) ---------


def _market_with(tmp_path, make_record, old=(), new=()):
    """A store holding ``old`` records seen 10 days ago and ``new`` records seen yesterday."""
    config = PricingIntelligenceConfig(database_path=str(tmp_path / "m.db"))
    store = ObservationStore(config.database_path)
    for run_id, age, rows in (("r-old", 10, old), ("r-new", 1, new)):
        when = NOW - timedelta(days=age)
        store.record_observations(
            [make_record(observed_at=when, **row) for row in rows],
            run_id=run_id,
            observed_at=when,
            product_type="home_loan",
        )
    return PricingIntelligenceService(config=config, store=store)


FRESH_OTHERS = [
    {"bank": b, "rate_pct": 4.89, "product_name": "Fixed Special"}
    for b in ("ANZ", "BNZ", "Westpac", "Kiwibank")
]


def test_a_name_excluded_product_is_not_reported_as_a_stale_exclusion(tmp_path, make_record):
    """ASB's niche 'Back My Build' is dropped from the market by name, never for staleness; its
    age must not make ASB look excluded or stale while ASB's ranked product is fresh."""
    asb_fresh = {"bank": "ASB", "rate_pct": 4.85, "product_name": "Fixed Special"}
    niche_old = {"bank": "ASB", "rate_pct": 4.20, "product_name": "Back My Build"}
    with _market_with(tmp_path, make_record, old=[niche_old], new=[asb_fresh, *FRESH_OTHERS]) as s:
        assert s.get_market_exclusions(now=NOW) == []
        assert "ASB" in s.get_market_snapshot(now=NOW).banks
        asb = next(b for b in assess_system_health(s, now=NOW).banks if b.bank == "ASB")
    assert asb.freshness.state is FreshnessState.FRESH


def test_a_name_excluded_product_alone_is_not_an_exclusion_either(tmp_path, make_record):
    niche_old = {"bank": "ASB", "rate_pct": 4.20, "product_name": "Back My Build"}
    with _market_with(tmp_path, make_record, old=[niche_old], new=FRESH_OTHERS) as s:
        assert s.get_market_exclusions(now=NOW) == []


def test_a_stale_sibling_is_not_an_exclusion_when_the_bank_has_a_fresh_rate(tmp_path, make_record):
    old_sibling = {"bank": "ASB", "rate_pct": 4.70, "product_name": "Fixed Special 80% LVR"}
    asb_fresh = {"bank": "ASB", "rate_pct": 4.85, "product_name": "Fixed Special"}
    with _market_with(
        tmp_path, make_record, old=[old_sibling], new=[asb_fresh, *FRESH_OTHERS]
    ) as s:
        assert s.get_market_exclusions(now=NOW) == []


def test_a_bank_with_only_stale_comparable_records_is_reported_once(tmp_path, make_record):
    old = [
        {"bank": "ASB", "rate_pct": 4.70, "product_name": "Fixed Special"},
        {"bank": "ASB", "rate_pct": 4.80, "product_name": "Fixed Special 80% LVR"},
    ]
    with _market_with(tmp_path, make_record, old=old, new=FRESH_OTHERS) as s:
        exclusions = s.get_market_exclusions(now=NOW)
    assert [(e.bank, e.term_months) for e in exclusions] == [("ASB", 12)]  # one per bank/term


def test_quarantined_observations_are_listed_by_bank_and_term(market_factory):
    market = market_factory(anz_new_rate=8.49, anz_new_status="warning")
    with PricingIntelligenceService(
        config=market.config, store=ObservationStore(market.database)
    ) as service:
        assert service.get_market_quarantined() == {("ANZ", 12)}
        assert "ANZ" in service.get_market_snapshot(now=market.now).banks  # fallback rate used


def test_a_healthy_market_has_nothing_quarantined(market_factory):
    market = market_factory()
    with PricingIntelligenceService(
        config=market.config, store=ObservationStore(market.database)
    ) as service:
        assert service.get_market_quarantined() == set()


@pytest.mark.parametrize(
    "product,category", [("home_loan", "special"), ("term_deposit", "term_deposit")]
)
@pytest.mark.parametrize(
    "age,expected",
    [(24, FreshnessState.FRESH), (240, FreshnessState.STALE), (None, FreshnessState.UNKNOWN)],
)
def test_quarantine_health_uses_trusted_fallback(
    tmp_path, make_record, product, category, age, expected
):
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "fallback.db"), selected_banks=["ANZ"]
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path)
    ) as service:
        if age is not None:
            when = NOW - timedelta(hours=age)
            service.store.record_observations(
                [
                    make_record(
                        bank="ANZ",
                        product_type=product,
                        rate_category=category,
                        observed_at=when,
                        rate_pct=4.5,
                    )
                ],
                run_id="trusted",
                observed_at=when,
                product_type=product,
            )
        # Repeated suspicious readings cannot refresh the fallback's confirmation time.
        for index in (2, 1):
            when = NOW - timedelta(hours=index)
            service.store.record_observations(
                [
                    make_record(
                        bank="ANZ",
                        product_type=product,
                        rate_category=category,
                        observed_at=when,
                        rate_pct=8.0 + index / 10,
                        validation_status="warning",
                    )
                ],
                run_id=f"warning-{index}",
                observed_at=when,
                product_type=product,
            )
        health = assess_system_health(service, product_type=product, now=NOW)
        bank = health.banks[0]
        assert bank.freshness.state is expected
        assert bank.freshness.quarantined
        assert bank.health is HealthLevel.DEGRADED
        assert bank.freshness.age_hours == age
        snapshot = service.get_market_snapshot(product_type=product, now=NOW)
        assert health.fresh_bank_count == len(snapshot.banks) == (1 if age == 24 else 0)
        if age == 24:
            assert snapshot.term(12).median_rate == 4.5


def test_health_resolves_category_flip_and_retains_mixed_term_age(tmp_path, make_record):
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "mixed.db"), selected_banks=["ANZ"]
    )
    with PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path)
    ) as service:
        for term, age in ((36, 240), (12, 24)):
            when = NOW - timedelta(hours=age)
            service.store.record_observations(
                [make_record(bank="ANZ", term_months=term, observed_at=when, rate_pct=4.5)],
                run_id=f"trusted-{term}",
                observed_at=when,
            )
        for index in (2, 1):
            when = NOW - timedelta(hours=index)
            service.store.record_observations(
                [
                    make_record(
                        bank="ANZ",
                        term_months=12,
                        rate_category="standard",
                        observed_at=when,
                        rate_pct=8.0 + index / 10,
                        validation_status="warning",
                    )
                ],
                run_id=f"flip-{index}",
                observed_at=when,
            )
        health = assess_system_health(service, now=NOW)
        assert health.banks[0].freshness.quarantined
        # Bank-wide health still uses the oldest trusted tracked-term confirmation.
        assert health.banks[0].freshness.state is FreshnessState.STALE
        assert health.banks[0].freshness.age_hours == 240
        snapshot = service.get_market_snapshot(now=NOW)
        assert snapshot.term(12).median_rate == 4.5
        assert 36 not in snapshot.term_months
