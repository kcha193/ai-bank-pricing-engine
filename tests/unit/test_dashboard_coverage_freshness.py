"""Dashboard trust presentation for DATA-003 (coverage) and DATA-004 (failed attempts).

Every figure comes from ``DashboardServices``; these tests check that the pages surface it: an
under-covered benchmark is labelled as observed data and refused for scenarios, and a failed
collection never reads as a successful refresh.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from dashboard_harness import make_services, run_page

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard.models import SCENARIO_JOINT, ScenarioRequest
from balance_pricing_engine.pricing_intelligence.coverage import CoverageStatus
from balance_pricing_engine.pricing_intelligence.store import ObservationStore, RunRecord

pytest.importorskip("streamlit")

THIN = ("ASB", "BNZ", "Westpac")  # stale in the fixture: ANZ and Kiwibank stay fresh


def _record_failed_attempt(market, *, product=ProductType.HOME_LOAN):
    at = market.now - timedelta(hours=1)
    with ObservationStore(market.database) as store:
        for bank in market.banks:
            store.update_collector_health(
                bank, product, success=False, error="ConnectionError", at=at, run_id="op-fail"
            )
        store.write_run_record(
            RunRecord(
                run_id="op-fail",
                started_at=at,
                completed_at=at + timedelta(minutes=1),
                status="FAILED",
                product_type=product,
                banks_attempted=market.banks,
                banks_successful=(),
                banks_failed=market.banks,
                rates_observed=0,
                rates_changed=0,
                validation_failures=0,
                suspicious_observations=0,
                market_state=None,
                error_summary="every bank failed",
            )
        )


# ---------------------------------------------------------------------- services


def test_term_summary_carries_coverage_and_oldest_input(market_factory):
    market = market_factory()
    summary = make_services(market).term_summary(12).value

    assert summary.coverage.eligible_bank_count == 5
    assert summary.coverage.min_banks == 3
    assert summary.coverage.oldest_input_age_hours == pytest.approx(24.0)
    assert summary.coverage.status is CoverageStatus.SUFFICIENT


def test_under_covered_term_summary_is_flagged_not_hidden(market_factory):
    market = market_factory(stale_banks=THIN)
    summary = make_services(market).term_summary(12).value

    assert summary.banks_observed == 2  # observed data stays visible
    assert not summary.coverage.sufficient
    assert summary.coverage.eligible_banks == ("ANZ", "Kiwibank")


def test_header_separates_rate_age_from_the_failed_attempt(market_factory):
    market = market_factory()
    _record_failed_attempt(market)

    header = make_services(market).header(ProductType.HOME_LOAN).value

    assert header.fresh_banks == 5  # the stored rates are inside the window
    assert header.confirmed_fresh_banks == 0  # but nothing was refreshed
    assert header.collection_status == "FAILED"
    assert header.collection_summary == "Latest collection failed: 5 of 5 banks failed"


def test_pricing_studio_refuses_an_under_covered_market_benchmark(market_factory):
    """T30: no trusted-looking result from a two-bank 'market'."""
    market = market_factory(stale_banks=THIN)
    result = make_services(market).scenarios(
        ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None)
    )

    assert result.status == "ERROR"
    assert result.value is None
    assert (
        "Insufficient competitor coverage for 12-month home loans: 2 eligible banks; "
        "minimum required: 3." in result.message
    )


def test_pricing_studio_manual_still_runs_without_the_market_set(market_factory):
    """T23 in the dashboard: MANUAL is an assumption, and says so."""
    market = market_factory(stale_banks=THIN)
    view = make_services(market).scenarios(
        ScenarioRequest(12, "MANUAL", 4.95, 0.0, None)
    ).value

    assert view.rows == ()
    assert view.selected.competitor_rate == pytest.approx(0.0495)
    assert view.loan_coverage is None and view.deposit_coverage is None
    assert "Insufficient competitor coverage" in view.coverage_warning
    assert "not a verified market benchmark" in view.coverage_warning


def test_pricing_studio_reports_benchmark_evidence(market_factory):
    market = market_factory()
    view = make_services(market).scenarios(
        ScenarioRequest(12, "MARKET_MEDIAN", None, 0.0, None)
    ).value

    assert view.loan_coverage.eligible_bank_count == 5
    assert view.deposit_coverage is None
    assert view.coverage_warning == ""


def test_joint_pricing_studio_checks_the_deposit_side_on_its_own(market_factory, make_record):
    """T28: a covered mortgage market never vouches for a one-bank deposit side."""
    market = market_factory()
    with ObservationStore(market.database) as store:
        store.record_observations(
            [
                make_record(
                    bank="ANZ",
                    product_type="term_deposit",
                    product_name="Term Deposit",
                    rate_category="term_deposit",
                    rate_pct=4.0,
                    observed_at=market.now - timedelta(hours=2),
                )
            ],
            run_id="td-1",
            observed_at=market.now - timedelta(hours=2),
            product_type="term_deposit",
        )
    result = make_services(market).scenarios(
        ScenarioRequest(
            12,
            "MARKET_MEDIAN",
            None,
            0.0,
            None,
            mode=SCENARIO_JOINT,
            deposit_strategy="MARKET_MEDIAN",
            deposit_term_months=12,
        )
    )

    assert result.status == "ERROR"
    assert result.value is None
    assert "12-month term deposits: 1 eligible bank; minimum required: 3." in result.message


# ------------------------------------------------------------------------- pages


def test_command_centre_warns_on_insufficient_coverage(market_factory):
    """T22."""
    market = market_factory(stale_banks=THIN)
    app = run_page("overview", market.database, market.now)

    assert not app.exception
    warnings = [w.value for w in app.warning]
    assert any("Insufficient competitor coverage for 12-month home loans" in w for w in warnings)
    assert any("not a trusted market benchmark" in w for w in warnings)


def test_market_explorer_warns_on_insufficient_coverage(market_factory):
    market = market_factory(stale_banks=THIN)
    app = run_page("market_watch", market.database, market.now)

    assert not app.exception
    assert any("2 eligible banks; minimum required: 3" in w.value for w in app.warning)


def test_covered_market_shows_evidence_caption_not_a_warning(market_factory):
    market = market_factory()
    app = run_page("market_watch", market.database, market.now)

    assert not app.exception
    assert not any("Insufficient competitor coverage" in w.value for w in app.warning)
    assert any(
        c.value.startswith("Benchmark evidence: 5 eligible banks (minimum 3)") for c in app.caption
    )


def test_data_health_shows_the_latest_operational_failure(market_factory):
    """T29."""
    market = market_factory()
    _record_failed_attempt(market)
    app = run_page("system_health", market.database, market.now)

    assert not app.exception
    assert any(
        "Latest collection failed: 5 of 5 banks failed" in w.value for w in app.warning
    )
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Fresh banks"] == "0 / 5"
    statuses = [
        frame.value
        for frame in app.dataframe
        if "Operational status" in frame.value.columns
    ][0]["Operational status"]
    assert set(statuses) == {
        "Degraded — last collection failed; latest trusted observations are 24 hours old"
    }


def test_pricing_studio_page_shows_benchmark_evidence(market_factory):
    market = market_factory()
    app = run_page("scenarios", market.database, market.now)
    app.button(key="scenario_run").click().run()

    assert not app.exception
    assert any(
        "Home loans benchmark evidence: 5 eligible banks (minimum 3)" in c.value
        for c in app.caption
    )
