import pytest
from dashboard_harness import make_services

from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.pricing_intelligence.health import FreshnessState
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore


def backend_snapshot(market):
    with PricingIntelligenceService(
        config=market.config, store=ObservationStore(market.database)
    ) as service:
        return service.get_market_snapshot(now=market.now)


def test_term_summary_equals_backend_values_and_ties(market_factory):
    market = market_factory()
    result = make_services(market).term_summary(12)
    truth = backend_snapshot(market).term(12)
    summary = result.value
    assert result.is_ok
    assert summary.leaders == truth.market_leaders == ("ANZ",)
    assert (
        (summary.lowest_pct, summary.median_pct, summary.highest_pct)
        == (
            truth.lowest_rate,
            truth.median_rate,
            truth.highest_rate,
        )
        == (4.79, 4.85, 4.89)
    )
    assert summary.spread_bps == truth.spread_bps == 10.0
    assert [(r.rank, r.bank) for r in summary.ranking] == [
        (1, "ANZ"),
        (2, "BNZ"),
        (2, "Westpac"),
        (4, "ASB"),
        (4, "Kiwibank"),
    ]
    assert summary.banks_observed == 5 and summary.fresh_banks == 5


def test_overview_matrix_marks_leader_and_stale_bank(market_factory):
    market = market_factory(stale_banks=("Westpac",))
    overview = make_services(market).overview().value
    cell = overview.matrix.cells
    assert cell[("ANZ", 12)].is_leader and cell[("ANZ", 12)].rate_pct == 4.79
    # stale: shown, marked, and not part of the live ranking
    assert cell[("Westpac", 12)].stale and cell[("Westpac", 12)].rate_pct == 4.85
    assert [e.bank for e in overview.exclusions] == ["Westpac"]
    assert overview.header.fresh_banks == 4 and overview.header.configured_banks == 5
    assert overview.changes_7d >= 1 and overview.dominant_direction == "DOWN"
    assert overview.default_term == 12


def test_bank_positions_use_backend_bps(market_factory):
    market = market_factory()
    positions = {p.bank: p for p in make_services(market).bank_positions(12).value}
    asb = positions["ASB"]
    assert (asb.rank, asb.vs_leader_bps, asb.vs_median_bps) == (4, 10.0, 4.0)
    assert asb.freshness is FreshnessState.FRESH
    assert positions["ANZ"].last_change_at is not None
    assert positions["ASB"].last_change_at is None


def test_single_bank_term_has_zero_spread_and_no_crash(market_factory):
    market = market_factory(stale_banks=("ANZ", "ASB", "BNZ", "Westpac"))  # only Kiwibank live
    summary = make_services(market).term_summary(12).value
    assert summary.banks_observed == 1 and summary.spread_bps == 0.0
    assert summary.leaders == ("Kiwibank",)


def test_unknown_term_is_an_empty_result(market_factory):
    result = make_services(market_factory()).term_summary(99)
    assert result.status == "EMPTY" and "99" in result.message


def test_missing_database_is_empty_and_not_created(tmp_path):
    missing = tmp_path / "nope.db"
    services = DashboardServices(load_settings({}, database_path=str(missing)))
    result = services.overview()
    assert result.status == "EMPTY" and "collect" in result.message.lower()
    assert not missing.exists()


def test_corrupt_database_is_an_operational_error_not_a_traceback(tmp_path, caplog):
    bad = tmp_path / "bad.db"
    bad.write_text("not a SQLite database")
    result = DashboardServices(load_settings({}, database_path=str(bad))).overview()
    assert result.status == "ERROR"
    assert "unavailable" in result.message.lower()
    assert "Traceback" not in result.message and "sqlite" not in result.message.lower()
    assert any(r.exc_info for r in caplog.records)  # detail went to the log


def test_empty_database_is_an_empty_result(tmp_path):
    empty = tmp_path / "empty.db"
    ObservationStore(empty).close()
    result = DashboardServices(load_settings({}, database_path=str(empty))).overview()
    assert result.status == "EMPTY"


@pytest.mark.parametrize("method", ["header", "overview"])
def test_every_read_degrades_on_a_missing_database(tmp_path, method):
    services = DashboardServices(load_settings({}, database_path=str(tmp_path / "x.db")))
    assert getattr(services, method)().status == "EMPTY"


def _record_failed_run(market, hours_after_now=-1):
    from datetime import timedelta

    from balance_pricing_engine.competitor_rates.models import ProductType
    from balance_pricing_engine.pricing_intelligence.store import RunRecord

    when = market.now + timedelta(hours=hours_after_now)
    store = ObservationStore(market.database)
    store.write_run_record(
        RunRecord(
            run_id="op-failed",
            started_at=when,
            completed_at=when + timedelta(minutes=1),
            status="FAILED",
            product_type=ProductType.HOME_LOAN,
            banks_attempted=market.banks,
            banks_successful=(),
            banks_failed=market.banks,
            rates_observed=0,
            rates_changed=0,
            validation_failures=0,
            suspicious_observations=0,
            market_state=None,
            error_summary="all banks unreachable",
        )
    )
    store.close()
    return when


def test_last_update_is_the_latest_run_that_produced_data_not_a_failed_one(market_factory):
    from datetime import timedelta

    market = market_factory()
    _record_failed_run(market)  # newer than every good run, but it collected nothing
    expected = market.now - timedelta(days=1) + timedelta(minutes=2)  # run-2's completion
    services = make_services(market)
    assert services.header().value.last_update == expected
    assert services.overview().value.header.last_update == expected


def test_last_update_counts_a_partial_run_because_it_did_store_data(market_factory):
    from datetime import timedelta

    market = market_factory(stale_banks=("Westpac",))  # newest run is PARTIAL_SUCCESS
    expected = market.now - timedelta(days=1) + timedelta(minutes=2)
    assert make_services(market).header().value.last_update == expected


def test_last_update_is_unknown_when_every_run_failed(tmp_path):
    from types import SimpleNamespace

    from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig

    database = tmp_path / "only-failures.db"
    market = SimpleNamespace(
        database=database,
        banks=("ANZ", "ASB"),
        now=__import__("datetime").datetime(
            2026, 10, 11, tzinfo=__import__("datetime").timezone.utc
        ),
        config=PricingIntelligenceConfig(database_path=str(database)),
    )
    ObservationStore(database).close()
    _record_failed_run(market)
    assert make_services(market).header().value.last_update is None


def test_overview_change_counts_exclude_suspicious_moves_like_the_market_state_does(
    market_factory,
):
    """Review finding: a quarantined jump was counted as a change while the rationale printed
    right under it said there were none."""
    market = market_factory(anz_new_rate=8.89, anz_new_status="warning")
    overview = make_services(market).overview().value
    assert (overview.changes_today, overview.changes_7d) == (0, 0)
    assert overview.banks_repricing_7d == 0
    assert "no rate changes" in overview.market_state_rationale


def test_overview_change_counts_agree_with_the_backend_activity(market_factory):
    market = market_factory()
    overview = make_services(market).overview().value
    with backend_service(market) as service:
        activity = service.get_market_activity(days=7, now=market.now)
    assert overview.changes_7d == activity.changes == 1
    assert overview.changes_today == 0  # the move was yesterday (NZ time)


def backend_service(market):
    return PricingIntelligenceService(config=market.config, store=ObservationStore(market.database))


@pytest.mark.parametrize(
    "case,expected",
    [
        ("other_term_only", 1),
        ("stale_selected_term", 1),
        ("mixed_terms", 2),
        ("duplicate_bank", 2),
        ("quarantined_only", 1),
        ("stale_trusted_fallback", 1),
    ],
)
def test_term_fresh_bank_count_uses_only_eligible_selected_term_observations(
    tmp_path, make_record, case, expected
):
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    database = tmp_path / "terms.db"
    store = ObservationStore(database)
    records = [make_record(bank="ANZ", observed_at=now)]
    if case in {"other_term_only", "stale_selected_term", "mixed_terms"}:
        records.append(make_record(bank="ASB", term_months=24, observed_at=now))
    if case in {"mixed_terms", "duplicate_bank"}:
        records.append(make_record(bank="ASB", observed_at=now))
    if case == "duplicate_bank":
        records.append(
            make_record(bank="ASB", product_name="Another Fixed Special", observed_at=now)
        )
    if case in {"stale_selected_term", "stale_trusted_fallback"}:
        stale = make_record(bank="ASB", observed_at=now - timedelta(hours=169))
        store.record_observations([stale], run_id="old", observed_at=stale.captured_at_utc)
    if case in {"quarantined_only", "stale_trusted_fallback"}:
        records.append(
            make_record(bank="ASB", rate_pct=8.49, validation_status="warning", observed_at=now)
        )
    store.record_observations(records, run_id="current", observed_at=now)
    store.close()
    services = DashboardServices(load_settings({}, database_path=str(database)), clock=lambda: now)
    result = services.term_summary(12)
    assert result.is_ok
    assert result.value.fresh_banks == expected
    assert result.value.fresh_banks <= result.value.banks_observed
    assert result.value.banks_observed == len({r.bank for r in result.value.ranking}) == expected
