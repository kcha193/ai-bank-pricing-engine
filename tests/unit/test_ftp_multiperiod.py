"""FIN-003 (Phase 4d): multi-period FTP roll with locked allocations (T42-T47)."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import math

import pytest

from balance_pricing_engine.config import MultiPeriodConfig, load_config
from balance_pricing_engine.governance.gates import DecisionStatus
from balance_pricing_engine.pricing.deposit_supply import (
    DepositMarketProfile,
    DepositOpportunitySegment,
)
from balance_pricing_engine.pricing.funds_transfer_pricing import (
    illustrative_curve,
    illustrative_funding_profile,
    scalar_equivalent_curve,
)
from balance_pricing_engine.scenarios import ftp_simulation
from balance_pricing_engine.scenarios.ftp_simulation import (
    FTPMultiPeriodSimulationConfig,
    PeriodFTPLedger,
    run_ftp_multiperiod_simulation,
)
from balance_pricing_engine.scenarios.multiperiod import run_multiperiod_diagnostics

START = datetime(2026, 11, 20, tzinfo=timezone.utc)
YEAR = timedelta(days=365)


@pytest.fixture(scope="module")
def engine():
    return load_config("configs/base.yaml").model_copy(
        update={"multiperiod": MultiPeriodConfig(n_periods=3, discount_rate=0.08)}
    )


def _schedule(*curves):
    times = tuple(START + YEAR * index + timedelta(hours=12) for index in range(len(curves)))
    return curves, times


def _config(engine, curves, **update):
    curves, times = _schedule(*curves)
    data = dict(
        engine_config=engine,
        curve_schedule=curves,
        valuation_times=times,
        funding_profile=illustrative_funding_profile(wholesale_funding_tenor_months=36),
        max_boundary_expansions=0,
    )
    data.update(update)
    return FTPMultiPeriodSimulationConfig(**data)


@pytest.fixture(scope="module")
def rising(engine):
    curves = tuple(
        illustrative_curve(
            START + YEAR * index,
            version=f"1.{index}.0",
            slope_scale=1 + index,
            premium_scale=1 + 0.25 * index,
        )
        for index in range(3)
    )
    return run_ftp_multiperiod_simulation(_config(engine, curves))


def test_t42_t43_booked_coupons_and_locked_ftp_survive_new_curves(rising):
    roll = rising.multiperiod
    rates = rising.period_rates
    assert len({rate.loan_ftp_rate for rate in rates}) == 3
    for cohort, diagnostics in zip(roll.booked_cohorts, roll.period_diagnostics):
        assert cohort.loan_rate == diagnostics.best_result.loan_rate
        assert cohort.deposit_rate == diagnostics.best_result.deposit_rate
    final = rising.period_ledgers[-1]
    for allocation in final.allocations:
        origin = rates[allocation.origination_period]
        assert allocation.loan_ftp_rate == origin.loan_ftp_rate
        assert allocation.deposit_ftp_rate == origin.deposit_ftp_rate
    survivors = [item for item in final.allocations if item.origination_period < 2]
    assert survivors and all(item.loan_ftp_rate != rates[2].loan_ftp_rate for item in survivors)
    economics = roll.period_economics[-1]
    for position in economics.active_cohorts:
        assert position.loan_interest_income == pytest.approx(
            position.loan_volume * roll.booked_cohorts[position.cohort.origination_period].loan_rate
        )


def test_t44_aggregate_funding_applies_once_at_the_period_curve(rising):
    for economics, rates, ledger in zip(
        rising.multiperiod.period_economics, rising.period_rates, rising.period_ledgers
    ):
        deficit = max(economics.total_active_loan_volume - economics.total_active_deposit_volume, 0)
        surplus = max(economics.total_active_deposit_volume - economics.total_active_loan_volume, 0)
        assert economics.wholesale_funding_cost == pytest.approx(
            deficit * rates.wholesale_funding_rate
        )
        assert economics.excess_deposit_income == pytest.approx(
            surplus * rates.surplus_reinvestment_rate
        )
        assert economics.wholesale_funding_rate == rates.wholesale_funding_rate
        per_cohort = math.fsum(
            max(position.loan_volume - position.deposit_volume, 0) * rates.wholesale_funding_rate
            for position in economics.active_cohorts
        )
        assert economics.wholesale_funding_cost <= per_cohort + 1e-6
        assert ledger.internal_transfer_net == 0.0
        assert ledger.consolidated_contribution == pytest.approx(
            ledger.external_economic_contribution, rel=1e-9, abs=1e-6
        )


def test_t45_discounted_period_contributions_reconcile(rising):
    roll = rising.multiperiod
    discounted = [ledger.discounted_model_objective for ledger in rising.period_ledgers]
    assert discounted == pytest.approx(roll.period_discounted_contributions, rel=1e-12)
    assert math.fsum(discounted) == pytest.approx(roll.npv, rel=1e-12)
    for index, ledger in enumerate(rising.period_ledgers):
        assert ledger.discount_factor == pytest.approx(1 / 1.08 ** (index + 1))
        assert ledger.model_objective == pytest.approx(
            roll.period_economics[index].active_portfolio_objective, rel=1e-9, abs=1e-6
        )
        json_ready = ledger.to_dict()
        assert json_ready["curve_key"] == rising.period_rates[index].curve_key


def test_t46_deposit_supply_roll_is_unsupported(engine):
    profile = DepositMarketProfile(
        profile_id="x",
        version="1",
        opportunity_basis="FLOW",
        review_notes="Illustrative.",
        segments=(DepositOpportunitySegment(segment_id="e", opportunity_nzd=1e8),),
    )
    curves = tuple(illustrative_curve(START + YEAR * i, version=f"1.{i}") for i in range(3))
    with pytest.raises(ValueError, match="multi-period stock-flow"):
        _config(engine, curves, deposit_supply_profile=profile)


def test_t47_profile_and_curve_version_lineage(rising, engine):
    provenance = rising.multiperiod.provenance
    assert provenance["ftp_curve_keys"] == [rate.curve_key for rate in rising.period_rates]
    assert len(set(provenance["ftp_curve_keys"])) == 3
    assert provenance["ftp_profile_key"] == "illustrative-12m-fixed@1.0.0"
    assert provenance["ftp_allocation_policy"] == "locked_at_origination"
    assert provenance["config_hash"] == rising.config_hash
    assert rising.effective_config.curve_schedule[1].version == "1.1.0"
    same_version = tuple(illustrative_curve(START + YEAR * i) for i in range(3))
    with pytest.raises(ValueError, match="ambiguous"):
        _config(engine, same_version)
    reused = illustrative_curve(START)
    assert (
        _config(
            engine,
            (reused, reused, reused),
            valuation_times=(
                START + timedelta(hours=1),
                START + timedelta(hours=2),
                START + timedelta(hours=3),
            ),
        ).curve_schedule[0]
        == reused
    )


def test_every_period_requires_review(rising):
    assert set(rising.period_governance_status) == {DecisionStatus.REVIEW_REQUIRED}
    assert rising.governance_status is DecisionStatus.REVIEW_REQUIRED
    for diagnostics in rising.multiperiod.period_diagnostics:
        assert diagnostics.ftp_market is not None


def test_any_blocked_period_blocks_the_roll(rising):
    statuses = (
        DecisionStatus.REVIEW_REQUIRED,
        DecisionStatus.BLOCKED,
        DecisionStatus.REVIEW_REQUIRED,
    )
    assert replace(rising, period_governance_status=statuses).governance_status is (
        DecisionStatus.BLOCKED
    )


def test_period_ledger_fails_loud_when_external_lines_disagree(rising, monkeypatch):
    economics = rising.multiperiod.period_economics[0]
    wrong = replace(
        rising.period_rates[0],
        wholesale_funding_rate=rising.period_rates[0].wholesale_funding_rate + 0.001,
    )
    with pytest.raises(ValueError, match="wholesale cost does not reconcile"):
        ftp_simulation._period_ledger(economics, (wrong, *rising.period_rates[1:]))
    monkeypatch.setattr(
        PeriodFTPLedger,
        "consolidated_contribution",
        property(lambda ledger: ledger.external_economic_contribution + 1.0),
    )
    with pytest.raises(ValueError, match="do not eliminate"):
        ftp_simulation._period_ledger(economics, rising.period_rates)


def test_scalar_equivalent_schedule_reproduces_the_phase_3b_roll(engine):
    curves = tuple(
        scalar_equivalent_curve(
            cash_rate=engine.market.cash_rate,
            wholesale_funding_spread=engine.market.wholesale_funding_spread,
            as_of=START + YEAR * index,
        ).model_copy(update={"version": f"1.{index}.0"})
        for index in range(3)
    )
    ftp = run_ftp_multiperiod_simulation(_config(engine, curves)).multiperiod
    legacy = run_multiperiod_diagnostics(engine, max_boundary_expansions=0)
    assert ftp.npv == legacy.npv
    for left, right in zip(ftp.period_economics, legacy.period_economics):
        assert asdict(left) == asdict(right)


def test_schedule_validation(engine):
    curves = tuple(illustrative_curve(START + YEAR * i, version=f"1.{i}") for i in range(3))
    with pytest.raises(ValueError, match="one curve and one valuation time per period"):
        _config(engine, curves[:2])
    with pytest.raises(ValueError, match="strictly increasing"):
        _config(engine, curves, valuation_times=(START, START, START))
    with pytest.raises(ValueError, match="requires engine_config.multiperiod"):
        _config(engine.model_copy(update={"multiperiod": None}), curves)
    with pytest.raises(ValueError, match="stale"):
        _config(
            engine,
            curves,
            valuation_times=tuple(START + YEAR * i + timedelta(days=30) for i in range(3)),
        )
