"""Replay of audit challenge cases H and I-b (DATA-003, DATA-004), plus the Phase 2a trust rules.

The original reproductions in ``docs/audits/2026-11-13-evidence/ingest/case_H_stale.py`` and
``case_I_td_zero_rows.py`` are evidence and are not edited. This replays the same shapes through
the same fixture HTML (same banks, page markup and business clock) and asserts the corrected
behaviour.

Before (audit, master at 16cb94e):
  H    day 8: only ANZ reachable; MARKET_MEDIAN -> 4.790% "median across 1 banks", as_of shows
       the newest input; day 6: 4 of 5 inputs 145 h old, nothing says so.
  I-b  seeded TD market, then every fetch fails: freshness frame "Fresh" for all five banks,
       fresh_bank_count 5.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from balance_pricing_engine.competitor_rates.collectors import COLLECTORS_BY_BANK, build_collectors
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence import dashboard_data
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.coverage import CoverageStatus
from balance_pricing_engine.pricing_intelligence.health import (
    OperationalState,
    RunHealth,
    assess_system_health,
)
from balance_pricing_engine.pricing_intelligence.orchestration import (
    EXIT_TOTAL_FAILURE,
    run_operational_cycle,
)
from balance_pricing_engine.pricing_intelligence.quarantine import ReviewAction
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_intelligence.strategy import InsufficientCoverageError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "pricing_intelligence"
BANKS = ["ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"]
T0 = datetime(2026, 11, 10, 18, 30, tzinfo=timezone.utc)
ASB_ROW = "<tr><td>12 Months</td><td>4.00% p.a.</td></tr>"


def _page(bank: str, product: ProductType) -> str:
    if product is ProductType.HOME_LOAN:
        return (FIXTURES / f"{bank.lower()}_home_loans.html").read_text()
    return (FIXTURES / "term_deposits_2026-10-30" / f"{bank.lower()}_term_deposits.html").read_text()


class FixtureMarket:
    """The audit harness's offline fetcher over the real fixture pages, one product."""

    def __init__(self, tmp_path, product: ProductType) -> None:
        self.product = product
        self.pages = {bank: _page(bank, product) for bank in BANKS}
        self.config = PricingIntelligenceConfig()
        self.config = self.config.model_copy(
            update={
                "collection": self.config.collection.model_copy(
                    update={"inter_request_delay_seconds": 0.0, "max_retries": 0}
                )
            }
        )
        self.raw_root = str(tmp_path / "raw")
        self.service = PricingIntelligenceService(
            config=self.config, store=ObservationStore(":memory:"), collectors=[], raw_root=self.raw_root
        )
        self.reachable(BANKS)

    def reachable(self, banks) -> None:
        def url(bank):
            collector = COLLECTORS_BY_BANK[bank]
            return (
                collector.home_loan_url
                if self.product is ProductType.HOME_LOAN
                else collector.term_deposit_url
            )

        live = {url(bank): self.pages[bank] for bank in banks}

        def fetch(address):
            if address not in live:
                raise ConnectionError(f"simulated outage for {address}")
            return live[address].encode("utf-8")

        self.service._collectors = build_collectors(
            banks=self.config.selected_banks,
            config=self.config.collection,
            fetcher=fetch,
            raw_root=self.raw_root,
        )

    def run(self, day: int):
        return run_operational_cycle(
            self.service,
            product_type=self.product,
            captured_at_utc=T0 + timedelta(days=day),
            run_id=f"run-{self.product.value}-{day}",
            alert_sinks={},
        )


# ------------------------------------------------------------------------- Case H (T31)


@pytest.mark.parametrize("product", [ProductType.HOME_LOAN, ProductType.TERM_DEPOSIT])
def test_case_h_enforces_minimum_coverage_and_reports_the_oldest_input(tmp_path, product):
    market = FixtureMarket(tmp_path, product)
    market.run(0)
    market.reachable(["ANZ"])  # from day 1 onwards only ANZ answers

    market.run(1)
    day1 = market.service.resolve_competitor_assumption(
        "MARKET_MEDIAN", 12, now=T0 + timedelta(days=1, hours=1), product_type=product
    )
    assert day1.coverage.eligible_bank_count == 5
    assert day1.coverage.oldest_input_age_hours == pytest.approx(25.0)
    assert day1.coverage.status is CoverageStatus.SUFFICIENT

    # Before: "median advertised rate across 5 banks", as_of = the newest (1 h old) input only.
    market.run(6)
    day6 = market.service.resolve_competitor_assumption(
        "MARKET_MEDIAN", 12, now=T0 + timedelta(days=6, hours=1), product_type=product
    )
    assert day6.as_of == T0 + timedelta(days=6)  # newest input: unchanged meaning
    assert day6.coverage.oldest_observed_at == T0
    assert day6.coverage.oldest_input_age_hours == pytest.approx(145.0)
    assert day6.coverage.status is CoverageStatus.SUFFICIENT_AGED
    assert "oldest input 145 h old" in day6.evidence_summary()

    # Before: MARKET_MEDIAN -> ANZ's own rate, "median advertised rate across 1 banks".
    market.run(8)
    noun = "home loans" if product is ProductType.HOME_LOAN else "term deposits"
    with pytest.raises(InsufficientCoverageError) as refused:
        market.service.resolve_competitor_assumption(
            "MARKET_MEDIAN", 12, now=T0 + timedelta(days=8, hours=1), product_type=product
        )
    assert str(refused.value) == (
        f"Insufficient competitor coverage for 12-month {noun}: 1 eligible bank; "
        "minimum required: 3."
    )
    # The thin view itself stays visible as observed data, with its coverage stated.
    coverage = market.service.get_benchmark_coverage(
        12, product_type=product, now=T0 + timedelta(days=8, hours=1)
    )
    assert coverage.eligible_banks == ("ANZ",)
    assert not coverage.sufficient


# ----------------------------------------------------------------------- Case I-b (T32)


def test_case_i_b_total_td_outage_reports_degraded_not_fresh(tmp_path):
    market = FixtureMarket(tmp_path, ProductType.TERM_DEPOSIT)
    market.run(0)
    market.reachable([])  # every TD fetch fails
    result = market.run(1)
    assert result.code == EXIT_TOTAL_FAILURE  # exit-code semantics unchanged

    now = T0 + timedelta(days=1, hours=1)
    service = market.service
    health = assess_system_health(service, product_type="term_deposit", now=now)
    assert health.run_health is RunHealth.FAILED
    assert health.fresh_bank_count == 5  # rate age: still inside the window
    assert health.confirmed_fresh_bank_count == 0  # Before: reported as 5 fresh
    expected = "Degraded — last collection failed; latest trusted observations are 25 hours old"
    assert {b.operational.label for b in health.banks} == {expected}

    store = service.store
    frame = dashboard_data.build_freshness_frame(
        store.get_current_observations(product_type="term_deposit"),
        BANKS,
        service.config.max_observation_age_hours,
        now,
        store.get_current_observation_run_ids(product_type="term_deposit"),
        store.latest_run_id(product_type="term_deposit"),
        latest_run=store.get_latest_run_record(product_type="term_deposit"),
        collector_health=store.get_collector_health(product_type="term_deposit"),
    )
    assert set(frame["Status"]) == {expected}  # Before: "Fresh" x5
    assert set(frame["Latest attempt status"]) == {"FAILED"}
    # Collector health stamps the wall clock of the successful day-0 attempt.
    assert frame["Last success (UTC)"].notna().all()

    # The home-loan side is untouched by the deposit outage (T07).
    loans = assess_system_health(service, product_type="home_loan", now=now)
    assert loans.run_health is RunHealth.UNKNOWN

    # Keep failing to day 8: the old readings age into STALE, still kept for audit (T04/T05).
    for day in range(2, 9):
        market.run(day)
    later = assess_system_health(
        service, product_type="term_deposit", now=T0 + timedelta(days=8, hours=1)
    )
    assert {b.operational.state for b in later.banks} == {OperationalState.STALE}
    assert len(store.get_current_observations(product_type="term_deposit")) > 0


# ------------------------------------------------- Phase 2a trust rules (T19, T20, T33)


def _asb_misread(tmp_path):
    market = FixtureMarket(tmp_path, ProductType.TERM_DEPOSIT)
    market.run(0)
    market.pages["ASB"] = market.pages["ASB"].replace(ASB_ROW, ASB_ROW.replace("4.00", "4.60"))
    market.reachable(BANKS)
    market.run(1)
    return market


def test_a_quarantined_reading_never_contributes_its_untrusted_rate(tmp_path):
    """T19: ASB counts once, through its last trusted rate; the misread stays out."""
    market = _asb_misread(tmp_path)
    now = T0 + timedelta(days=1, hours=1)

    snapshot = market.service.get_market_snapshot(product_type="term_deposit", now=now)
    asb = next(e for e in snapshot.term(12).entries if e.bank == "ASB")
    assert (asb.rate_pct, asb.quarantined) == (4.00, True)
    resolved = market.service.resolve_competitor_assumption(
        "MARKET_LEADER", 12, now=now, product_type="term_deposit"
    )
    assert resolved.rate_pct != 4.60
    assert resolved.coverage.eligible_bank_count == 5


def test_an_approved_reading_is_eligible(tmp_path):
    """T20."""
    market = _asb_misread(tmp_path)
    now = T0 + timedelta(days=1, hours=1)
    (case,) = market.service.list_review_cases(ProductType.TERM_DEPOSIT, now=now)
    market.service.review(
        case.event_id,
        ReviewAction.APPROVE,
        reviewer="operator",
        reason="confirmed against the bank's rate card",
        evidence_ref="https://rates.example.test/asb/card.pdf",
        reviewed_at=now,
    )

    snapshot = market.service.get_market_snapshot(product_type="term_deposit", now=now)
    asb = next(e for e in snapshot.term(12).entries if e.bank == "ASB")
    assert (asb.rate_pct, asb.quarantined) == (4.60, False)
    resolved = market.service.resolve_competitor_assumption(
        "MARKET_LEADER", 12, now=now, product_type="term_deposit"
    )
    assert resolved.rate_pct == 4.60
    assert "ASB" in resolved.coverage.eligible_banks


def test_a_bank_with_only_untrusted_readings_does_not_count(tmp_path, make_record):
    """T19: no trusted fallback means no bank -- coverage is never padded by a warning row."""
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(), store=ObservationStore(":memory:"), collectors=[]
    )
    service.store.record_observations(
        [
            make_record(bank="ANZ", observed_at=now),
            make_record(bank="BNZ", observed_at=now),
            make_record(bank="ASB", observed_at=now, validation_status="warning"),
        ],
        observed_at=now,
    )

    coverage = service.get_benchmark_coverage(12, now=now)
    assert coverage.eligible_banks == ("ANZ", "BNZ")
    with pytest.raises(InsufficientCoverageError, match="2 eligible banks"):
        service.resolve_competitor_assumption("MARKET_MEDIAN", 12, now=now)


def test_historical_as_of_views_never_use_future_observations(tmp_path, make_record):
    """T25: a reconstruction ages its inputs against its own run, not today."""
    first = datetime(2026, 11, 1, tzinfo=timezone.utc)
    later = first + timedelta(days=3)
    service = PricingIntelligenceService(
        config=PricingIntelligenceConfig(), store=ObservationStore(":memory:"), collectors=[]
    )
    service.store.record_observations(
        [make_record(bank=b, observed_at=first, rate_pct=4.8) for b in ("ANZ", "ASB", "BNZ")],
        run_id="first",
        observed_at=first,
    )
    service.store.record_observations(
        [make_record(bank=b, observed_at=later, rate_pct=4.5) for b in ("ANZ", "ASB", "BNZ")],
        run_id="later",
        observed_at=later,
    )

    historical = service.get_market_snapshot(as_of_run_id="first")
    coverage = service.get_benchmark_coverage(12, snapshot=historical)

    assert historical.evaluated_at == first
    assert coverage.evaluated_at == first
    assert coverage.newest_observed_at == first
    assert coverage.oldest_input_age_hours == pytest.approx(0.0)
    assert historical.term(12).median_rate == 4.8
