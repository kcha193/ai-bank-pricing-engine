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
