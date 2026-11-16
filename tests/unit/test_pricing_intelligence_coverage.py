"""Benchmark coverage and oldest-input evidence for market-derived assumptions (DATA-003).

A market-derived competitor assumption must come from enough *distinct, eligible* banks, and it
must say how old its oldest input is. These tests cover the configuration, the coverage
assessment and its enforcement at the single strategy seam every caller goes through.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from balance_pricing_engine.config import load_config
from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.coverage import (
    DEFAULT_MIN_BANKS,
    CoverageStatus,
    assess_benchmark_coverage,
)
from balance_pricing_engine.pricing_intelligence.snapshot import build_market_snapshot
from balance_pricing_engine.pricing_intelligence.strategy import (
    InsufficientCoverageError,
    MarketStrategyError,
    apply_market_strategy_to_config,
    resolve_competitor_rate,
    resolve_market_overlay,
)

NOW = datetime(2026, 11, 16, 18, 30, tzinfo=timezone.utc)


def _td(make_record, bank, rate, *, age_hours=1.0, **overrides):
    return make_record(
        bank=bank,
        rate_pct=rate,
        product_type="term_deposit",
        product_name=overrides.pop("product_name", "Term Deposit"),
        rate_category=overrides.pop("rate_category", "term_deposit"),
        observed_at=NOW - timedelta(hours=age_hours),
        **overrides,
    )


def _hl(make_record, bank, rate, *, age_hours=1.0, **overrides):
    return make_record(
        bank=bank, rate_pct=rate, observed_at=NOW - timedelta(hours=age_hours), **overrides
    )


def _hl_snapshot(records):
    return build_market_snapshot(records, product_type="home_loan", evaluated_at=NOW)


def _td_snapshot(records, **kwargs):
    return build_market_snapshot(
        records,
        product_type="term_deposit",
        rate_category="term_deposit",
        evaluated_at=NOW,
        **kwargs,
    )


# ------------------------------------------------------------------ T11/T12 configuration


def test_min_banks_defaults_to_three_for_a_five_bank_market():
    config = PricingIntelligenceConfig()

    assert DEFAULT_MIN_BANKS == 3
    assert config.min_banks == 3
    assert len(config.selected_banks) == 5


@pytest.mark.parametrize("value", [1, 2, 3, 5])
def test_min_banks_accepts_valid_values(value):
    assert PricingIntelligenceConfig(min_banks=value).min_banks == value


@pytest.mark.parametrize("value", [0, -1, 2.5, "three"])
def test_min_banks_rejects_invalid_values(value):
    with pytest.raises(ValidationError):
        PricingIntelligenceConfig(min_banks=value)


def test_an_explicit_min_banks_above_the_tracked_banks_is_rejected():
    with pytest.raises(ValidationError, match="min_banks"):
        PricingIntelligenceConfig(selected_banks=["ANZ", "ASB"], min_banks=3)


def test_a_small_tracked_set_with_the_default_min_banks_still_loads():
    """The default is a policy, not a config error: under-coverage fails at resolution."""
    config = PricingIntelligenceConfig(selected_banks=["ANZ"])
    assert config.min_banks == 3


def test_min_banks_is_configurable_from_yaml(tmp_path):
    text = open("configs/base.yaml").read()
    text += "\npricing_intelligence:\n  min_banks: 4\n  aged_input_hours: 30\n"
    path = tmp_path / "engine.yaml"
    path.write_text(text)

    config = load_config(str(path))

    assert config.pricing_intelligence.min_banks == 4
    assert config.pricing_intelligence.aged_input_hours == 30


def test_aged_input_hours_must_be_positive():
    with pytest.raises(ValidationError):
        PricingIntelligenceConfig(aged_input_hours=0)


# ------------------------------------------------------------- coverage counting (T15-T21)


def test_five_banks_count_five(make_record):
    snapshot = _hl_snapshot(
        [_hl(make_record, b, 4.8 + i / 100) for i, b in enumerate(["ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"])]
    )

    coverage = assess_benchmark_coverage(snapshot, 12, min_banks=3)

    assert coverage.eligible_bank_count == 5
    assert coverage.status is CoverageStatus.SUFFICIENT
    assert coverage.sufficient


def test_several_records_from_one_bank_count_once(make_record):
    """T16: rates, LVR bands and payment-frequency variants from one bank are one bank."""
    records = [
        _td(make_record, "ANZ", 4.00, interest_payment_frequency="at_maturity"),
        _td(make_record, "ANZ", 3.95, interest_payment_frequency="monthly"),
        _td(make_record, "ANZ", 3.90, product_name="Term Deposit (quarterly)"),
        _td(make_record, "ANZ", 3.85, product_name="Term Deposit (annual)"),
        _td(make_record, "ANZ", 3.80, product_name="Term Deposit (six-monthly)"),
    ]

    coverage = assess_benchmark_coverage(_td_snapshot(records), 12, min_banks=3)

    assert coverage.eligible_bank_count == 1
    assert coverage.eligible_banks == ("ANZ",)
    assert coverage.status is CoverageStatus.INSUFFICIENT


def test_own_bank_exclusion_reduces_eligible_coverage(make_record):
    """T17."""
    snapshot = _hl_snapshot(
        [_hl(make_record, b, 4.8) for b in ("ANZ", "ASB", "BNZ")]
    )

    assert assess_benchmark_coverage(snapshot, 12, min_banks=3).sufficient
    coverage = assess_benchmark_coverage(snapshot, 12, min_banks=3, our_bank=" asb ")

    assert coverage.eligible_banks == ("ANZ", "BNZ")
    assert coverage.own_bank_excluded
    assert not coverage.sufficient


def test_a_comparable_td_special_keeps_its_phase_2b_treatment(make_record):
    """T21: an admitted special counts as its bank's representative; nothing else changes."""
    special = _td(make_record, "BNZ", 4.40, rate_category="special", product_name="Special")
    records = [
        _td(make_record, "ANZ", 4.00),
        special,
        _td(make_record, "ASB", 4.05),
    ]

    admitted = _td_snapshot(records, admitted_keys={special.observation_key()})
    not_admitted = _td_snapshot(records)

    covered = assess_benchmark_coverage(admitted, 12, min_banks=3)
    assert covered.eligible_banks == ("ANZ", "ASB", "BNZ")
    assert covered.sufficient
    # An unadmitted (restricted or uncertain) special never adds a bank, even to reach min_banks.
    assert assess_benchmark_coverage(not_admitted, 12, min_banks=3).eligible_banks == ("ANZ", "ASB")


def test_missing_term_has_zero_eligible_banks(make_record):
    coverage = assess_benchmark_coverage(_hl_snapshot([_hl(make_record, "ANZ", 4.8)]), 24, min_banks=1)

    assert coverage.eligible_bank_count == 0
    assert coverage.status is CoverageStatus.INSUFFICIENT
    assert coverage.oldest_observed_at is None


# ----------------------------------------------------------------- oldest input (T24/T25)


def test_oldest_input_age_is_computed_against_the_evaluation_time(make_record):
    snapshot = _hl_snapshot(
        [
            _hl(make_record, "ANZ", 4.79, age_hours=1),
            _hl(make_record, "BNZ", 4.85, age_hours=145),
            _hl(make_record, "ASB", 4.89, age_hours=25),
        ]
    )

    coverage = assess_benchmark_coverage(snapshot, 12, min_banks=3, aged_input_hours=48)

    assert coverage.evaluated_at == NOW
    assert coverage.oldest_observed_at == NOW - timedelta(hours=145)
    assert coverage.oldest_input_age_hours == pytest.approx(145.0)
    assert coverage.newest_observed_at == NOW - timedelta(hours=1)
    assert coverage.status is CoverageStatus.SUFFICIENT_AGED
    assert "145" in coverage.describe()


def test_an_explicit_evaluation_time_overrides_the_snapshot(make_record):
    snapshot = _hl_snapshot([_hl(make_record, b, 4.8, age_hours=10) for b in ("ANZ", "ASB", "BNZ")])

    later = NOW + timedelta(hours=10)
    coverage = assess_benchmark_coverage(snapshot, 12, min_banks=3, now=later)

    assert coverage.oldest_input_age_hours == pytest.approx(20.0)


def test_a_snapshot_without_an_evaluation_time_uses_its_as_of_not_the_wall_clock(make_record):
    """T25: historical reconstruction must never age inputs against today's clock."""
    as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snapshot = build_market_snapshot(
        [
            make_record(bank=b, rate_pct=4.8, observed_at=as_of - timedelta(hours=6))
            for b in ("ANZ", "ASB", "BNZ")
        ],
        as_of=as_of,
    )

    coverage = assess_benchmark_coverage(snapshot, 12, min_banks=3)

    assert coverage.evaluated_at == as_of
    assert coverage.oldest_input_age_hours == pytest.approx(6.0)


# ------------------------------------------------------------ enforcement at the seam


def test_one_bank_median_is_rejected_under_a_three_bank_minimum(make_record):
    """T13."""
    snapshot = _td_snapshot([_td(make_record, "ANZ", 4.00)])

    with pytest.raises(InsufficientCoverageError) as raised:
        resolve_competitor_rate("MARKET_MEDIAN", snapshot, 12, min_banks=3)

    assert str(raised.value) == (
        "Insufficient competitor coverage for 12-month term deposits: 1 eligible bank; "
        "minimum required: 3."
    )
    assert isinstance(raised.value, MarketStrategyError)
    assert raised.value.coverage.eligible_banks == ("ANZ",)


def test_two_bank_leader_is_rejected_under_a_three_bank_minimum(make_record):
    """T14."""
    snapshot = _hl_snapshot([_hl(make_record, "ANZ", 4.79), _hl(make_record, "BNZ", 4.85)])

    with pytest.raises(InsufficientCoverageError, match="12-month home loans: 2 eligible banks"):
        resolve_competitor_rate("MARKET_LEADER", snapshot, 12, min_banks=3)


@pytest.mark.parametrize(
    "strategy", ["MARKET_MEDIAN", "MARKET_MEAN", "MARKET_LEADER", "MARKET_SECOND_LOWEST"]
)
def test_every_market_strategy_enforces_coverage(make_record, strategy):
    snapshot = _hl_snapshot([_hl(make_record, "ANZ", 4.79), _hl(make_record, "BNZ", 4.85)])

    with pytest.raises(InsufficientCoverageError):
        resolve_competitor_rate(strategy, snapshot, 12, min_banks=3)


def test_the_default_policy_requires_three_banks_for_direct_callers(make_record):
    snapshot = _hl_snapshot([_hl(make_record, "ANZ", 4.79), _hl(make_record, "BNZ", 4.85)])

    with pytest.raises(InsufficientCoverageError, match="minimum required: 3"):
        resolve_competitor_rate("MARKET_MEDIAN", snapshot, 12)


def test_three_eligible_banks_satisfy_a_three_bank_minimum(make_record):
    """T15, plus the evidence carried on the resolved assumption (T24)."""
    snapshot = _hl_snapshot(
        [
            _hl(make_record, "ANZ", 4.79, age_hours=1),
            _hl(make_record, "BNZ", 4.85, age_hours=145),
            _hl(make_record, "ASB", 4.89, age_hours=25),
        ]
    )

    resolved = resolve_competitor_rate("MARKET_MEDIAN", snapshot, 12, min_banks=3)

    assert resolved.rate_pct == 4.85
    coverage = resolved.coverage
    assert coverage.eligible_bank_count == 3
    assert coverage.min_banks == 3
    assert coverage.eligible_banks == ("ANZ", "ASB", "BNZ")
    assert coverage.oldest_input_age_hours == pytest.approx(145.0)
    assert coverage.status is CoverageStatus.SUFFICIENT_AGED
    assert resolved.as_of == snapshot.as_of  # newest input, unchanged meaning
    assert "oldest input 145 h" in resolved.evidence_summary()


def test_own_bank_exclusion_can_make_a_strategy_fail(make_record):
    snapshot = _hl_snapshot([_hl(make_record, b, 4.8) for b in ("ANZ", "ASB", "BNZ")])

    with pytest.raises(InsufficientCoverageError, match="2 eligible banks"):
        resolve_competitor_rate("MARKET_MEDIAN", snapshot, 12, min_banks=3, our_bank="ASB")


def test_manual_strategy_needs_no_snapshot_and_discloses_it_is_an_assumption():
    """T23."""
    resolved = resolve_competitor_rate("MANUAL", None, 12, manual_rate=0.05, min_banks=5)

    assert resolved.coverage is None
    assert not resolved.is_market_benchmark
    assert "assumption" in resolved.evidence_summary()
    assert "not a verified market benchmark" in resolved.evidence_summary()


def test_insufficient_coverage_never_falls_back_to_the_configured_rate(make_record):
    config = load_config("configs/base.yaml")
    snapshot = _hl_snapshot([_hl(make_record, "ANZ", 4.79)])

    with pytest.raises(InsufficientCoverageError):
        apply_market_strategy_to_config(config, strategy="MARKET_MEDIAN", snapshot=snapshot)


def test_overlay_reads_min_banks_from_the_engine_config(make_record):
    config = load_config("configs/base.yaml").model_copy(
        update={"pricing_intelligence": PricingIntelligenceConfig(min_banks=1)}
    )
    snapshot = _hl_snapshot([_hl(make_record, "ANZ", 4.79)])

    overlay = resolve_market_overlay(config, strategy="MARKET_MEDIAN", snapshot=snapshot)

    assert overlay.config.market.competitor_loan_rate == pytest.approx(0.0479)
    assert overlay.loan.coverage.min_banks == 1
    assert overlay.deposit is None


def test_an_explicit_min_banks_overrides_the_engine_config(make_record):
    config = load_config("configs/base.yaml").model_copy(
        update={"pricing_intelligence": PricingIntelligenceConfig(min_banks=1)}
    )
    snapshot = _hl_snapshot([_hl(make_record, "ANZ", 4.79)])

    with pytest.raises(InsufficientCoverageError):
        apply_market_strategy_to_config(
            config, strategy="MARKET_MEDIAN", snapshot=snapshot, min_banks=2
        )


def test_joint_overlay_validates_each_side_independently(make_record):
    """T28: a covered loan side cannot carry an under-covered deposit side."""
    config = load_config("configs/base.yaml")
    loans = _hl_snapshot([_hl(make_record, b, 4.8) for b in ("ANZ", "ASB", "BNZ")])
    thin_deposits = _td_snapshot([_td(make_record, "ANZ", 4.0)])
    deposits = _td_snapshot([_td(make_record, b, 4.0) for b in ("ANZ", "ASB", "BNZ")])

    with pytest.raises(InsufficientCoverageError, match="term deposits: 1 eligible bank"):
        apply_market_strategy_to_config(
            config,
            strategy="MARKET_MEDIAN",
            snapshot=loans,
            deposit_strategy="MARKET_MEDIAN",
            deposit_snapshot=thin_deposits,
        )
    thin_loans = _hl_snapshot([_hl(make_record, "ANZ", 4.8)])
    with pytest.raises(InsufficientCoverageError, match="home loans: 1 eligible bank"):
        apply_market_strategy_to_config(
            config,
            strategy="MARKET_MEDIAN",
            snapshot=thin_loans,
            deposit_strategy="MARKET_MEDIAN",
            deposit_snapshot=deposits,
        )

    overlay = resolve_market_overlay(
        config,
        strategy="MARKET_MEDIAN",
        snapshot=loans,
        deposit_strategy="MARKET_MEDIAN",
        deposit_snapshot=deposits,
    )
    assert overlay.loan.coverage.product_type is ProductType.HOME_LOAN
    assert overlay.deposit.coverage.product_type is ProductType.TERM_DEPOSIT


def test_manual_overlay_returns_the_same_config_object(make_record):
    config = load_config("configs/base.yaml")
    thin = _hl_snapshot([_hl(make_record, "ANZ", 4.79)])

    assert apply_market_strategy_to_config(config, strategy="MANUAL", snapshot=thin) is config
    overlay = resolve_market_overlay(config, strategy="MANUAL", snapshot=thin)
    assert overlay.config is config
    assert overlay.loan.coverage is None


def test_a_covered_overlay_prices_exactly_as_before(make_record):
    """T35: coverage is a gate, not an input -- a passing overlay is the same config as the
    equivalent explicit rate, so optimiser outputs for identical assumptions cannot move."""
    from balance_pricing_engine.pricing_intelligence.strategy import (
        apply_manual_competitor_rate_to_config,
    )

    config = load_config("configs/base.yaml")
    snapshot = _hl_snapshot(
        [_hl(make_record, b, r) for b, r in [("ANZ", 4.79), ("BNZ", 4.85), ("ASB", 4.89)]]
    )

    overlay = apply_market_strategy_to_config(config, strategy="MARKET_MEDIAN", snapshot=snapshot)
    explicit = apply_manual_competitor_rate_to_config(config, 0.0485)

    assert overlay.market == explicit.market
    assert overlay.response == config.response
    assert overlay.model_dump(exclude={"market"}) == config.model_dump(exclude={"market"})
