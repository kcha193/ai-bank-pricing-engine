"""Collected term deposits reach the dashboard: collector -> store -> DashboardServices -> pages.

The store is filled by the real collection path (the 2026-10-30 live page structures served
offline), not by hand-built records, so a parsing, categorisation, persistence or read defect
anywhere along the way shows up as an empty or wrong Term Deposits view.
"""

from datetime import datetime, timedelta, timezone

import pytest
from dashboard_harness import frame_with, run_page

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard.services import DashboardServices
from balance_pricing_engine.dashboard.settings import load_settings
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.orchestration import run_operational_cycle
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore

BANKS = {"ANZ", "ASB", "BNZ", "Kiwibank", "Westpac"}
NOW = datetime.now(timezone.utc).replace(microsecond=0)
#: Advertised 1-year rates on the 2026-10-30 pages. Deposits rank highest first: BNZ and
#: Kiwibank tie for the lead.
ONE_YEAR = {"ANZ": 4.00, "ASB": 4.00, "BNZ": 4.05, "Kiwibank": 4.05, "Westpac": 4.00}


@pytest.fixture
def collected(tmp_path, fixture_fetcher):
    """A store holding one home-loan and one term-deposit operational run, as the dashboard's
    Run Pricing Watch now produces."""
    database = tmp_path / "observations.db"
    config = PricingIntelligenceConfig(
        database_path=str(database), collection=CollectionConfig(inter_request_delay_seconds=0)
    )
    statuses = {}
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(database),
        fetcher=fixture_fetcher(),
        raw_root=tmp_path / "raw",  # never the repository's data/raw/
    ) as service:
        for product in (ProductType.HOME_LOAN, ProductType.TERM_DEPOSIT):
            result = run_operational_cycle(service, product_type=product)
            statuses[product] = result.status
    return database, statuses


def services(database):
    return DashboardServices(
        load_settings({}, database_path=str(database)), clock=lambda: NOW + timedelta(minutes=1)
    )


def test_both_collections_succeed_and_are_recorded_per_product(collected):
    database, statuses = collected
    assert statuses == {ProductType.HOME_LOAN: "SUCCESS", ProductType.TERM_DEPOSIT: "SUCCESS"}
    reads = services(database)
    for product in statuses:
        runs = reads.runs(product).value
        assert [run.status for run in runs] == ["SUCCESS"]


def test_services_read_current_term_deposit_observations(collected):
    reads = services(collected[0])
    summary = reads.term_summary(12, ProductType.TERM_DEPOSIT)
    assert summary.status == "OK", summary.message
    positions = reads.bank_positions(12, ProductType.TERM_DEPOSIT).value
    assert {p.bank: p.rate_pct for p in positions} == ONE_YEAR
    assert reads.exclusions(ProductType.TERM_DEPOSIT).value == ()


def test_home_loan_reads_are_unchanged_by_the_deposit_collection(collected):
    reads = services(collected[0])
    loans = reads.bank_positions(12, ProductType.HOME_LOAN).value
    assert {p.bank for p in loans} == BANKS
    deposit_rates = {p.rate_pct for p in reads.bank_positions(12, ProductType.TERM_DEPOSIT).value}
    assert not {p.rate_pct for p in loans} & deposit_rates


def test_market_explorer_shows_the_collected_deposit_market(collected):
    app = run_page("market_watch", collected[0], NOW + timedelta(minutes=1))
    app.segmented_control(key="market_watch_product").set_value("term_deposit").run()
    assert not app.exception
    ranking = frame_with(app, "Rank")
    assert set(ranking["Bank"]) == BANKS
    leaders = set(ranking.loc[ranking["Rank"] == 1, "Bank"])
    assert leaders == {"BNZ", "Kiwibank"}


def test_command_centre_shows_the_collected_deposit_leader(collected):
    app = run_page("overview", collected[0], NOW + timedelta(minutes=1))
    assert not app.exception
    values = {m.label: m.value for m in app.metric}
    assert values["Highest rate"] == "4.05%"
    assert "No term deposit market data yet" not in " ".join(m.value for m in app.markdown)


def test_market_trends_render_the_collected_deposit_history(collected):
    app = run_page("trends", collected[0], NOW + timedelta(minutes=1))
    app.segmented_control(key="trends_product").set_value("term_deposit").run()
    assert not app.exception
    assert any(h.value == "Term Deposit Market Trends" for h in app.header)
    assert not [i.value for i in app.info if "No " in i.value and "data" in i.value]
