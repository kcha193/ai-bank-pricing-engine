import pytest

from dashboard_harness import make_services

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard.models import ChangeWindow
from balance_pricing_engine.pricing_intelligence.health import (
    CollectorStatus,
    FreshnessState,
    HealthLevel,
)


def test_health_is_the_backend_model_not_a_recomputation(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    health = make_services(market).health().value
    westpac = next(b for b in health.banks if b.bank == "Westpac")
    assert westpac.freshness.state is FreshnessState.STALE
    assert westpac.collector is CollectorStatus.FAILING
    assert westpac.consecutive_failures == 2 and westpac.health is HealthLevel.DEGRADED
    assert health.max_observation_age_hours == 168.0
    assert health.fresh_bank_count == 4


def test_health_of_a_missing_database_is_an_empty_result(tmp_path):
    from balance_pricing_engine.dashboard.services import DashboardServices
    from balance_pricing_engine.dashboard.settings import load_settings

    services = DashboardServices(load_settings({}, database_path=str(tmp_path / "x.db")))
    assert services.health().status == "EMPTY"


def test_exclusions_explain_the_stale_bank(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    exclusions = make_services(market).exclusions().value
    assert [(e.bank, round(e.age_hours), e.threshold_hours) for e in exclusions] == [
        ("Westpac", 240, 168.0)
    ]


def test_runs_are_newest_first_with_duration_and_counts(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    rows = make_services(market).runs().value
    assert [r.status for r in rows] == ["PARTIAL_SUCCESS", "PARTIAL_SUCCESS", "SUCCESS"]
    newest = rows[0]
    assert (newest.banks_ok, newest.banks_failed, newest.observations) == (4, 1, 4)
    assert newest.duration_seconds == 120.0
    detail = make_services(market).run_detail(newest.run_id).value
    assert detail.banks_failed == ("Westpac",) and detail.market_state == "NORMAL"


def test_runs_for_a_product_with_no_history_is_an_empty_result(market_factory):
    result = make_services(market_factory()).runs(ProductType.TERM_DEPOSIT)
    assert result.status == "EMPTY" and "No operational runs" in result.message


def test_unknown_run_id_is_an_empty_result(market_factory):
    assert make_services(market_factory()).run_detail("nope").status == "EMPTY"


def test_suspicious_observations_are_listed_apart_from_trusted_rates(market_factory):
    market = market_factory(anz_new_rate=8.49, anz_new_status="warning")
    services = make_services(market)
    rows = services.suspicious().value
    assert [(r.bank, r.previous_pct, r.observed_pct, r.change_bps) for r in rows] == [
        ("ANZ", 4.89, 8.49, 360.0)
    ]
    trusted = services.overview().value.matrix.cells[("ANZ", 12)]
    assert trusted.rate_pct == 4.89  # the trusted benchmark never sees the 8.49 reading
    assert (
        services.changes(ChangeWindow("30d")).status == "EMPTY"
    )  # and Rate Changes does not present it as a real move


def test_no_suspicious_observations_is_an_empty_result(market_factory):
    assert make_services(market_factory()).suspicious().status == "EMPTY"


@pytest.mark.parametrize(
    "mortgage,deposit", [(True, False), (False, True), (True, True), (False, False)]
)
def test_quarantines_are_product_scoped(quarantine_market_factory, mortgage, deposit):
    market = quarantine_market_factory(mortgage, deposit)
    services = make_services(market)
    for product, expected, bank in (
        (ProductType.HOME_LOAN, mortgage, "ANZ"),
        (ProductType.TERM_DEPOSIT, deposit, "ASB"),
    ):
        result = services.suspicious(product)
        assert result.status == ("OK" if expected else "EMPTY")
        assert [r.bank for r in result.value or ()] == ([bank] if expected else [])
    if deposit:
        from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
        from balance_pricing_engine.pricing_intelligence.store import ObservationStore

        with PricingIntelligenceService(
            config=market.config, store=ObservationStore(market.database)
        ) as backend:
            term = backend.get_market_snapshot(ProductType.TERM_DEPOSIT, now=market.now).term(12)
            assert term.rate_for("ASB") == 4.0
            assert (
                backend.get_market_activity(
                    product_type=ProductType.TERM_DEPOSIT, now=market.now
                ).changes
                == 0
            )
