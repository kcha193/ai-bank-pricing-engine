"""FIN-003 (Phase 4d): term-matched FTP curve validation, lookup and tenor matching."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math

from hypothesis import given
from hypothesis import strategies as st
import pytest
from pydantic import ValidationError

from balance_pricing_engine.pricing.funds_transfer_pricing import (
    FTPAdjustment,
    FTPCurve,
    FTPCurvePoint,
    FTPLiquidityBucket,
    ILLUSTRATIVE_TENORS_MONTHS,
    Interpolation,
    RateUnit,
    illustrative_curve,
    illustrative_funding_profile,
    scalar_equivalent_curve,
)

AS_OF = datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)
VALUATION = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)


def _points(*rows):
    return tuple(
        FTPCurvePoint(tenor_months=tenor, reference_rate=reference, liquidity_premium=premium)
        for tenor, reference, premium in rows
    )


def _curve(**update) -> FTPCurve:
    data = dict(
        curve_id="test-curve",
        version="1",
        currency="NZD",
        as_of=AS_OF,
        source="Synthetic test fixture",
        instrument="Illustrative NZD term reference proxy",
        rate_unit=RateUnit.FRACTION,
        compounding="annual_simple",
        max_age_days=5,
        points=_points((3, 0.030, 0.002), (12, 0.032, 0.004), (36, 0.034, 0.008)),
    )
    data.update(update)
    return FTPCurve(**data)


def test_t01_exact_tenor_lookup_returns_the_quoted_components():
    quote = _curve().quote(12)
    assert quote.reference_rate == 0.032
    assert quote.liquidity_premium == 0.004
    assert quote.all_in_rate == 0.032 + 0.004
    assert quote.interpolated is False
    assert quote.bracket_months is None


def test_t02_linear_interpolation_is_documented_and_disclosed():
    curve = _curve(interpolation=Interpolation.LINEAR_IN_TENOR)
    quote = curve.quote(24)
    assert quote.interpolated is True
    assert quote.bracket_months == (12, 36)
    assert quote.reference_rate == pytest.approx(0.033, abs=1e-15)
    assert quote.liquidity_premium == pytest.approx(0.006, abs=1e-15)


def test_exact_only_curve_refuses_an_unquoted_tenor():
    with pytest.raises(ValueError, match="not quoted"):
        _curve().quote(24)


def test_t03_duplicate_tenors_are_rejected_explicitly():
    with pytest.raises(ValidationError, match="Duplicate FTP tenor 12"):
        _curve(points=_points((12, 0.03, 0.0), (12, 0.031, 0.0)))


def test_f01_tenors_must_be_strictly_increasing():
    with pytest.raises(ValidationError, match="strictly increasing"):
        _curve(points=_points((12, 0.03, 0.0), (3, 0.031, 0.0)))


def test_t04_missing_required_tenor_is_never_substituted():
    with pytest.raises(ValidationError, match="Required FTP tenor 24 months is missing"):
        _curve(required_tenors_months=(12, 24), interpolation=Interpolation.LINEAR_IN_TENOR)


def test_t05_stale_curve_is_rejected_at_valuation():
    curve = _curve(max_age_days=1)
    with pytest.raises(ValueError, match="stale"):
        curve.check_valid_at(AS_OF + timedelta(days=1, seconds=1))
    curve.check_valid_at(AS_OF + timedelta(days=1))


def test_t06_future_dated_quote_is_look_ahead():
    with pytest.raises(ValueError, match="look-ahead"):
        _curve().check_valid_at(AS_OF - timedelta(seconds=1))


def test_valuation_and_as_of_must_be_timezone_aware():
    with pytest.raises(ValidationError):
        _curve(as_of=datetime(2026, 11, 20))
    with pytest.raises(ValueError, match="timezone-aware"):
        _curve().check_valid_at(datetime(2026, 11, 21))


def test_f07_curve_key_versions_dates():
    first = _curve()
    later = _curve(as_of=AS_OF + timedelta(days=1))
    assert first.key != later.key
    assert first.key == "test-curve@1#2026-11-20T17:00:00+00:00"


def test_t07_currency_mismatch_is_rejected():
    with pytest.raises(ValidationError, match="Currency AUD is not supported"):
        _curve(currency="AUD")


@pytest.mark.parametrize(
    ("unit", "value"),
    [(RateUnit.FRACTION, 3.2), (RateUnit.PERCENT, 320.0), (RateUnit.BASIS_POINTS, 32_000.0)],
)
def test_t08_implausible_magnitude_for_the_declared_unit_is_ambiguous(unit, value):
    with pytest.raises(ValidationError, match="ambiguous"):
        _curve(rate_unit=unit, points=_points((12, value, 0.0)))


def test_t08_rate_unit_is_mandatory():
    with pytest.raises(ValidationError, match="rate_unit"):
        FTPCurve(
            curve_id="c",
            version="1",
            currency="NZD",
            as_of=AS_OF,
            source="s",
            instrument="i",
            compounding="annual_simple",
            max_age_days=1,
            points=_points((12, 0.03, 0.0)),
        )


def test_units_convert_to_fractions_without_changing_the_quote():
    percent = _curve(rate_unit=RateUnit.PERCENT, points=_points((12, 3.2, 0.4)))
    bps = _curve(rate_unit=RateUnit.BASIS_POINTS, points=_points((12, 320.0, 40.0)))
    for curve in (percent, bps):
        quote = curve.quote(12)
        assert quote.reference_rate == pytest.approx(0.032, abs=1e-15)
        assert quote.liquidity_premium == pytest.approx(0.004, abs=1e-15)


def test_f10_unsupported_compounding_is_rejected():
    with pytest.raises(ValidationError, match="Compounding convention 'continuous'"):
        _curve(compounding="continuous")


def test_t09_extrapolation_is_rejected_by_default():
    curve = _curve(interpolation=Interpolation.LINEAR_IN_TENOR)
    with pytest.raises(ValueError, match="extrapolation"):
        curve.quote(60)
    with pytest.raises(ValueError, match="extrapolation"):
        curve.quote(1)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_t10_non_finite_quotes_are_rejected(bad):
    with pytest.raises(ValidationError):
        _curve(points=_points((12, bad, 0.0)))
    with pytest.raises(ValidationError):
        _curve(points=_points((12, 0.03, bad)))


def test_negative_rates_need_an_explicit_policy():
    with pytest.raises(ValidationError, match="Negative reference rate"):
        _curve(points=_points((12, -0.001, 0.002)))
    curve = _curve(points=_points((12, -0.001, 0.002)), allow_negative_rates=True)
    assert curve.quote(12).reference_rate == -0.001


def test_liquidity_premium_cannot_be_negative():
    with pytest.raises(ValidationError, match="Liquidity premium"):
        _curve(points=_points((12, 0.03, -0.001)))


def test_t11_source_evidence_is_required():
    with pytest.raises(ValidationError):
        _curve(source="  ")
    with pytest.raises(ValidationError):
        _curve(instrument="")


def test_t12_curve_valuation_is_deterministic_and_reproducible():
    curve = illustrative_curve(AS_OF)
    profile = illustrative_funding_profile()
    first = profile.rates(curve)
    second = profile.rates(FTPCurve.model_validate_json(curve.model_dump_json()))
    assert first == second


def test_adjustments_are_named_signed_and_added_once():
    point = FTPCurvePoint(
        tenor_months=12,
        reference_rate=0.03,
        liquidity_premium=0.004,
        adjustments=(
            FTPAdjustment(name="basis", value=0.0005, rationale="Illustrative basis."),
            FTPAdjustment(name="buffer_cost", value=-0.0002, rationale="Illustrative."),
        ),
    )
    quote = _curve(points=(point,)).quote(12)
    assert quote.adjustment_total == pytest.approx(0.0003, abs=1e-15)
    assert quote.all_in_rate == pytest.approx(0.0343, abs=1e-15)
    with pytest.raises(ValidationError, match="Duplicate adjustment"):
        FTPCurvePoint(
            tenor_months=12,
            reference_rate=0.03,
            liquidity_premium=0.0,
            adjustments=(
                FTPAdjustment(name="basis", value=0.0, rationale="a"),
                FTPAdjustment(name="basis", value=0.0, rationale="b"),
            ),
        )


def test_interpolation_requires_matching_adjustments():
    first = FTPCurvePoint(
        tenor_months=12,
        reference_rate=0.03,
        liquidity_premium=0.0,
        adjustments=(FTPAdjustment(name="basis", value=0.001, rationale="a"),),
    )
    second = FTPCurvePoint(tenor_months=36, reference_rate=0.03, liquidity_premium=0.0)
    curve = _curve(points=(first, second), interpolation=Interpolation.LINEAR_IN_TENOR)
    with pytest.raises(ValueError, match="same adjustments"):
        curve.quote(24)


@given(
    left=st.floats(0.0, 0.10),
    right=st.floats(0.0, 0.10),
    tenor=st.integers(13, 35),
)
def test_interpolated_quotes_stay_between_their_brackets(left, right, tenor):
    curve = _curve(
        interpolation=Interpolation.LINEAR_IN_TENOR,
        points=_points((12, left, 0.0), (36, right, 0.0)),
    )
    quote = curve.quote(tenor)
    assert min(left, right) - 1e-15 <= quote.reference_rate <= max(left, right) + 1e-15


# -- Contractual versus behavioural tenors (Section 6, T28) ----------------------------


def test_t28_repricing_and_liquidity_tenors_are_distinct_components():
    curve = illustrative_curve(AS_OF)
    profile = illustrative_funding_profile()
    rates = profile.rates(curve)
    repricing = curve.quote(profile.loan_repricing_tenor_months)
    assert rates.loan_reference_rate == repricing.reference_rate
    expected_liquidity = math.fsum(
        bucket.weight
        * (
            curve.quote(bucket.tenor_months).liquidity_premium
            + curve.quote(bucket.tenor_months).adjustment_total
        )
        for bucket in profile.loan_liquidity_buckets
    )
    assert rates.loan_liquidity_rate == pytest.approx(expected_liquidity, abs=1e-15)
    # A 12-month fixed mortgage is not funded as if it matured after 12 months.
    assert rates.loan_liquidity_rate > repricing.liquidity_premium
    longer = profile.model_copy(
        update={"loan_liquidity_buckets": (FTPLiquidityBucket(tenor_months=60, weight=1.0),)}
    )
    shifted = longer.rates(curve)
    assert shifted.loan_reference_rate == rates.loan_reference_rate
    assert shifted.loan_liquidity_rate != rates.loan_liquidity_rate
    other_repricing = profile.model_copy(update={"loan_repricing_tenor_months": 36})
    assert other_repricing.rates(curve).loan_liquidity_rate == rates.loan_liquidity_rate


def test_liquidity_buckets_must_sum_to_one_and_be_unique():
    with pytest.raises(ValidationError, match="sum to one"):
        illustrative_funding_profile().model_copy(
            update={"loan_liquidity_buckets": (FTPLiquidityBucket(tenor_months=12, weight=0.5),)}
        )
    with pytest.raises(ValidationError, match="unique"):
        illustrative_funding_profile().model_copy(
            update={
                "loan_liquidity_buckets": (
                    FTPLiquidityBucket(tenor_months=12, weight=0.5),
                    FTPLiquidityBucket(tenor_months=12, weight=0.5),
                )
            }
        )


def test_deposit_credit_uses_contractual_maturity_only():
    profile = illustrative_funding_profile()
    assert profile.deposit_renewal_basis == "contractual_maturity_only"
    with pytest.raises(ValidationError):
        profile.model_copy(update={"deposit_renewal_basis": "behavioural_rollover"})
    rates = profile.rates(illustrative_curve(AS_OF))
    quote = illustrative_curve(AS_OF).quote(profile.deposit_contractual_tenor_months)
    assert rates.deposit_ftp_rate == quote.all_in_rate


def test_profile_tenors_must_be_resolvable_on_the_curve():
    profile = illustrative_funding_profile().model_copy(
        update={"wholesale_funding_tenor_months": 48}
    )
    with pytest.raises(ValueError, match="48"):
        profile.rates(illustrative_curve(AS_OF))


@pytest.mark.parametrize("spread", [0.012, 0.0, -0.001])
def test_scalar_equivalent_curve_reproduces_the_legacy_rates_exactly(spread):
    curve = scalar_equivalent_curve(cash_rate=0.025, wholesale_funding_spread=spread, as_of=AS_OF)
    rates = illustrative_funding_profile().rates(curve)
    assert rates.wholesale_funding_rate == 0.025 + spread
    assert rates.surplus_reinvestment_rate == 0.025
    assert curve.source_classification == "SYNTHETIC / UNCALIBRATED"
    assert curve.allow_negative_rates is False
    assert tuple(point.tenor_months for point in curve.points) == ILLUSTRATIVE_TENORS_MONTHS
    for point in curve.points:
        assert curve.quote(point.tenor_months).all_in_rate == 0.025 + spread
        assert point.liquidity_premium == max(spread, 0.0)
        if spread < 0:
            (adjustment,) = point.adjustments
            assert adjustment.name == "legacy_negative_wholesale_spread"
            assert adjustment.value == spread
            assert adjustment.rationale
        else:
            assert point.adjustments == ()


def test_scalar_equivalent_curve_explicitly_allows_a_negative_cash_reference():
    curve = scalar_equivalent_curve(cash_rate=-0.005, wholesale_funding_spread=-0.001, as_of=AS_OF)
    assert curve.allow_negative_rates is True
    assert curve.quote(12).all_in_rate == -0.005 - 0.001


@pytest.mark.parametrize("spread", [math.nan, math.inf, -math.inf])
def test_scalar_equivalent_curve_rejects_nonfinite_spreads(spread):
    with pytest.raises(ValidationError, match="finite"):
        scalar_equivalent_curve(cash_rate=0.025, wholesale_funding_spread=spread, as_of=AS_OF)
