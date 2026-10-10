from datetime import date, datetime, timedelta, timezone

from dashboard_harness import make_services

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.pricing_intelligence.changes import ChangeType, RateChange
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.store import ObservationStore


def test_history_series_and_market_stats_come_from_backend_math(market_factory):
    market = market_factory()
    data = make_services(market).history(12, ("ANZ", "BNZ"), days=None).value
    anz = [(p.day, p.rate_pct) for p in data.points if p.bank == "ANZ"]
    # points exist where a new advertised state was recorded: first sight, then the move
    assert anz == [(date(2026, 10, 1), 4.89), (date(2026, 10, 10), 4.79)]
    assert {p.bank for p in data.points} == {"ANZ", "BNZ"}  # bank filter applies to lines only
    first, latest = data.market[0], data.market[-1]
    assert (first.median_pct, first.leader_pct, first.spread_bps) == (4.89, 4.85, 4.0)
    assert (latest.median_pct, latest.leader_pct, latest.spread_bps) == (4.85, 4.79, 10.0)
    assert latest.leaders == ("ANZ",) and first.leaders == ("BNZ", "Westpac")


def test_timeline_is_a_factual_sequence_with_offsets(market_factory):
    market = market_factory()
    events = make_services(market).history(12, ("ANZ",), days=30).value.timeline
    assert [(e.bank, e.change_bps, e.offset_days) for e in events] == [("ANZ", -10.0, 0.0)]


def test_history_not_blanked_by_a_different_tiers_withdrawal(tmp_path, make_record):
    """Ported: a bank withdrawing its "standard" product must not blank the "special"
    line's still-valid history -- withdrawal handling must be scoped to the rate category."""
    day_one = datetime.now(timezone.utc) - timedelta(days=3)
    day_two = datetime.now(timezone.utc) - timedelta(days=2)
    later_day = day_one + timedelta(days=2)
    path = tmp_path / "observations.db"
    config = PricingIntelligenceConfig(database_path=str(path))
    store = ObservationStore(path)
    store.record_observations(
        [
            make_record(bank="ANZ", rate_category="special", rate_pct=4.79),
            make_record(bank="BNZ", rate_category="special", rate_pct=4.85),
        ],
        observed_at=day_one,
    )
    store.record_observations(
        [make_record(bank="BNZ", rate_category="special", rate_pct=4.75)], observed_at=later_day
    )
    store.record_changes(
        [
            RateChange(
                observation_key="ANZ|home_loan|standard|12|unknown|fixed standard",
                bank="ANZ",
                product_type=ProductType.HOME_LOAN,
                product_name="Fixed Standard",
                rate_category="standard",
                term_months=12,
                change_type=ChangeType.REMOVED_PRODUCT,
                detected_at=day_two,
                run_id="test-run",
            )
        ]
    )
    store.close()

    services = DashboardServices(load_settings({}, database_path=str(path)))
    assert config.database_path == str(path)
    data = services.history(12, ("ANZ", "BNZ"), days=None).value
    on_later_day = {p.bank: p.rate_pct for p in data.points if p.day == later_day.date()}
    assert on_later_day["ANZ"] == 4.79


def test_history_with_no_data_in_range_is_empty(market_factory):
    market = market_factory()
    assert make_services(market).history(60, ("ANZ",), days=30).status == "EMPTY"


def test_history_range_drops_older_points(market_factory):
    market = market_factory()
    data = make_services(market).history(12, ("ANZ",), days=5).value
    assert [p.day for p in data.points] == [date(2026, 10, 10)]
