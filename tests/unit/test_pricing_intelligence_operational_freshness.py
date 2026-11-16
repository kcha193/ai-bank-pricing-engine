"""Operational freshness: observation age and the latest collection attempt (DATA-004).

Observation freshness ("how old are the rates?") and collection health ("did the latest attempt
succeed?") are separate facts. A failed attempt never writes ``collection_runs`` (no records
reach persistence), so only the ``operational_runs`` ledger can tell a dashboard that the most
recent attempt failed. These tests pin the combined state and keep the two facts separate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence import dashboard_data
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.health import (
    CollectionOutcome,
    FreshnessState,
    OperationalState,
    RunHealth,
    assess_observation_freshness,
    assess_operational_freshness,
    assess_system_health,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore, RunRecord

NOW = datetime(2026, 11, 16, 18, 30, tzinfo=timezone.utc)
BANKS = ("ANZ", "ASB", "BNZ")


def _run(status, *, ok=BANKS, failed=(), at=NOW - timedelta(hours=1), run_id="op-1", product="home_loan"):
    return RunRecord(
        run_id=run_id,
        started_at=at,
        completed_at=at + timedelta(minutes=2),
        status=status,
        product_type=ProductType(product),
        banks_attempted=tuple(ok) + tuple(failed),
        banks_successful=tuple(ok),
        banks_failed=tuple(failed),
        rates_observed=len(ok),
        rates_changed=0,
        validation_failures=0,
        suspicious_observations=0,
        market_state=None,
        error_summary=None,
    )


def _observation(make_record, age_hours, *, status="unvalidated"):
    records = [
        make_record(
            bank="ANZ", observed_at=NOW - timedelta(hours=age_hours), validation_status=status
        )
    ]
    return assess_observation_freshness(
        records, "ANZ", now=NOW, max_age_hours=168, run_ids=["c-1"], latest_run_id="c-1"
    )


# ------------------------------------------------------------------- the shared evaluator


def test_successful_latest_run_supports_fresh(make_record):
    """T01."""
    result = assess_operational_freshness(_observation(make_record, 1), _run("SUCCESS"))

    assert result.state is OperationalState.FRESH
    assert result.collection is CollectionOutcome.SUCCEEDED
    assert result.latest_attempt_at == NOW - timedelta(hours=1)
    assert result.latest_attempt_status == "SUCCESS"
    assert result.label == "Fresh"


def test_failed_latest_run_is_degraded_even_with_recent_observations(make_record):
    """T02: the observations are within the window; the attempt still failed."""
    observation = _observation(make_record, 25)
    result = assess_operational_freshness(
        observation, _run("FAILED", ok=(), failed=BANKS), last_success_at=NOW - timedelta(hours=25)
    )

    assert observation.state is FreshnessState.FRESH  # the fact about the rates is unchanged
    assert result.state is OperationalState.DEGRADED
    assert result.collection is CollectionOutcome.FAILED
    assert result.last_success_at == NOW - timedelta(hours=25)
    assert result.label == (
        "Degraded — last collection failed; latest trusted observations are 25 hours old"
    )


def test_expired_observations_are_stale_whatever_the_latest_attempt(make_record):
    """T05."""
    for run in (_run("SUCCESS"), _run("FAILED", ok=(), failed=BANKS), None):
        result = assess_operational_freshness(_observation(make_record, 200), run)
        assert result.state is OperationalState.STALE
        assert result.label.startswith("Stale")


def test_no_operational_history_is_unknown_never_inferred_fresh(make_record):
    """T06: old observation rows alone are not evidence of a successful collection."""
    result = assess_operational_freshness(_observation(make_record, 1), None)

    assert result.state is OperationalState.UNKNOWN
    assert result.collection is CollectionOutcome.UNKNOWN
    assert "no operational collection run recorded" in result.reason


def test_no_observation_at_all_is_unknown(make_record):
    missing = assess_observation_freshness(
        [], "ANZ", now=NOW, max_age_hours=168, run_ids=[], latest_run_id=None
    )
    result = assess_operational_freshness(missing, _run("FAILED", ok=(), failed=BANKS))

    assert result.state is OperationalState.UNKNOWN
    assert result.collection is CollectionOutcome.FAILED


def test_a_bank_succeeding_inside_a_partial_run_is_fresh(make_record):
    result = assess_operational_freshness(
        _observation(make_record, 1), _run("PARTIAL_SUCCESS", ok=("ANZ",), failed=("ASB",))
    )

    assert result.state is OperationalState.FRESH


def test_a_bank_missing_from_the_latest_attempt_is_degraded(make_record):
    result = assess_operational_freshness(
        _observation(make_record, 1), _run("SUCCESS", ok=("ASB", "BNZ"))
    )

    assert result.state is OperationalState.DEGRADED
    assert result.collection is CollectionOutcome.NOT_ATTEMPTED


def test_quarantine_keeps_its_phase_2a_state(make_record):
    result = assess_operational_freshness(
        _observation(make_record, 1, status="warning"), _run("SUCCESS")
    )

    assert result.state is OperationalState.QUARANTINED
    assert result.label == "Quarantined readings"


# --------------------------------------------------------------- end-to-end through the store


def _service(tmp_path, **config):
    config = PricingIntelligenceConfig(
        database_path=str(tmp_path / "obs.db"), selected_banks=list(BANKS), **config
    )
    return PricingIntelligenceService(
        config=config, store=ObservationStore(config.database_path), collectors=[]
    )


def _baseline(service, make_record, product="home_loan", *, at):
    kwargs = (
        {"product_type": "term_deposit", "rate_category": "term_deposit", "product_name": "TD"}
        if product == "term_deposit"
        else {}
    )
    records = [make_record(bank=b, observed_at=at, rate_pct=4.0, **kwargs) for b in BANKS]
    service.store.record_observations(
        records, run_id=f"c-{product}-{at:%d%H}", observed_at=at, product_type=product
    )
    for bank in BANKS:
        service.store.update_collector_health(bank, product, success=True, error=None, at=at)
    service.store.write_run_record(_run("SUCCESS", at=at, run_id=f"op-{product}-{at:%d%H}", product=product))


def _fail(service, product="home_loan", *, at, run_id=None, status="FAILED", failed=BANKS):
    for bank in failed:
        service.store.update_collector_health(
            bank, product, success=False, error="ConnectionError", at=at
        )
    ok = tuple(b for b in BANKS if b not in failed)
    service.store.write_run_record(
        _run(status, ok=ok, failed=failed, at=at, run_id=run_id or f"op-f-{at:%d%H}", product=product)
    )


def test_total_outage_after_a_successful_baseline_is_not_fresh(tmp_path, make_record):
    """T03/T04/T10: observations kept for audit, timestamps untouched, state degraded."""
    service = _service(tmp_path)
    t0 = NOW - timedelta(hours=25)
    _baseline(service, make_record, "term_deposit", at=t0)
    _fail(service, "term_deposit", at=NOW - timedelta(minutes=30))

    health = assess_system_health(service, product_type="term_deposit", now=NOW)

    assert health.run_health is RunHealth.FAILED
    assert health.confirmed_fresh_bank_count == 0
    assert health.fresh_bank_count == 3  # the rates themselves are still inside the window
    for bank in health.banks:
        assert bank.operational.state is OperationalState.DEGRADED
        assert bank.operational.latest_attempt_status == "FAILED"
        assert bank.operational.observation.oldest_confirmation == t0
    stored = service.store.get_current_observations(product_type="term_deposit")
    assert len(stored) == 3 and {r.captured_at_utc for r in stored} == {t0}
    assert health.collection_summary == "Latest collection failed: 3 of 3 banks failed"


def test_mortgage_and_deposit_status_stay_independent(tmp_path, make_record):
    """T07: one failed TD attempt never touches the mortgage view, and vice versa."""
    service = _service(tmp_path)
    _baseline(service, make_record, "home_loan", at=NOW - timedelta(hours=2))
    _baseline(service, make_record, "term_deposit", at=NOW - timedelta(hours=2))
    _fail(service, "term_deposit", at=NOW - timedelta(hours=1))

    loans = assess_system_health(service, product_type="home_loan", now=NOW)
    deposits = assess_system_health(service, product_type="term_deposit", now=NOW)

    assert loans.run_health is RunHealth.HEALTHY
    assert loans.confirmed_fresh_bank_count == 3
    assert deposits.run_health is RunHealth.FAILED
    assert deposits.confirmed_fresh_bank_count == 0


def test_partial_success_is_visible_as_degraded(tmp_path, make_record):
    """T08."""
    service = _service(tmp_path)
    _baseline(service, make_record, at=NOW - timedelta(hours=3))
    _fail(service, at=NOW - timedelta(hours=1), status="PARTIAL_SUCCESS", failed=("BNZ",))

    health = assess_system_health(service, now=NOW)

    assert health.run_health is RunHealth.DEGRADED
    states = {b.bank: b.operational.state for b in health.banks}
    assert states["BNZ"] is OperationalState.DEGRADED
    assert health.collection_summary == "Latest collection partially succeeded: 1 of 3 banks failed"


def test_retrying_a_run_id_does_not_fabricate_freshness(tmp_path, make_record):
    """T09: a replayed run record changes no observation and no age."""
    service = _service(tmp_path)
    t0 = NOW - timedelta(hours=30)
    _baseline(service, make_record, at=t0)
    attempt = NOW - timedelta(hours=1)
    _fail(service, at=attempt, run_id="op-retry")
    _fail(service, at=attempt, run_id="op-retry")
    # A SUCCESS ledger row with no new observation is still not new observation evidence.
    service.store.write_run_record(_run("SUCCESS", at=attempt, run_id="op-retry"))

    health = assess_system_health(service, now=NOW)

    assert len(service.store.get_recent_run_records(limit=10)) == 2
    for bank in health.banks:
        assert bank.freshness.age_hours == pytest.approx(30.0)
        assert bank.operational.observation.oldest_confirmation == t0


def test_legacy_database_without_operational_runs_reports_unknown(tmp_path, make_record):
    """T06 through the store: no ledger row, only observation rows."""
    service = _service(tmp_path)
    at = NOW - timedelta(hours=1)
    service.store.record_observations(
        [make_record(bank=b, observed_at=at) for b in BANKS],
        run_id="c-legacy",
        observed_at=at,
        product_type="home_loan",
    )

    health = assess_system_health(service, now=NOW)

    assert health.run_health is RunHealth.UNKNOWN
    assert health.confirmed_fresh_bank_count == 0
    assert all(b.operational.state is OperationalState.UNKNOWN for b in health.banks)
    assert health.collection_summary == "No collection run recorded"


def test_oldest_contributing_observation_is_reported(tmp_path, make_record):
    service = _service(tmp_path)
    _baseline(service, make_record, at=NOW - timedelta(hours=40))

    health = assess_system_health(service, now=NOW)

    assert health.oldest_observation_age_hours == pytest.approx(40.0)


# ----------------------------------------------------------------- the freshness frame


def test_freshness_frame_reports_the_failed_attempt(make_record):
    records = [make_record(bank=b, observed_at=NOW - timedelta(hours=25)) for b in BANKS]
    attempt = _run("FAILED", ok=(), failed=BANKS)

    frame = dashboard_data.build_freshness_frame(
        records, list(BANKS), 168, NOW, {b: ["c-1"] for b in BANKS}, "c-1", latest_run=attempt
    )

    assert set(frame["Status"]) == {
        "Degraded — last collection failed; latest trusted observations are 25 hours old"
    }
    assert set(frame["Latest attempt status"]) == {"FAILED"}
    assert set(frame["Latest attempt (UTC)"]) == {attempt.started_at}
    assert set(frame["Age (hours)"]) == {25.0}


def test_freshness_frame_without_attempt_evidence_keeps_its_legacy_columns(make_record):
    records = [make_record(bank="ANZ", observed_at=NOW - timedelta(hours=2))]

    frame = dashboard_data.build_freshness_frame(records, ["ANZ"], 168, NOW, {"ANZ": ["r"]}, "r")

    assert list(frame.columns) == ["Oldest confirmation (UTC)", "Age (hours)", "Status"]
