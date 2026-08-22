"""Validation stops a parser fault from becoming market data."""

import pytest

from balance_pricing_engine.pricing_intelligence.config import ValidationThresholds
from balance_pricing_engine.pricing_intelligence.validation import validate_observations


def test_a_plausible_observation_is_accepted(make_record):
    outcome = validate_observations([make_record(rate_pct=4.79)])

    assert len(outcome.accepted) == 1
    assert outcome.rejected == []
    assert outcome.errors == []


@pytest.mark.parametrize("rate", [0.1, 19.99, -1.0, 0.0])
def test_an_implausible_rate_is_rejected(make_record, rate):
    outcome = validate_observations([make_record(rate_pct=rate)])

    assert outcome.accepted == []
    assert len(outcome.rejected) == 1
    assert outcome.errors[0].code == "rate_out_of_range"


def test_rate_bounds_are_configurable(make_record):
    thresholds = ValidationThresholds(min_mortgage_rate_pct=4.0, max_mortgage_rate_pct=6.0)

    assert validate_observations([make_record(rate_pct=7.5)], thresholds).rejected
    assert validate_observations([make_record(rate_pct=5.5)], thresholds).accepted


def test_an_invalid_term_is_rejected(make_record):
    outcome = validate_observations(
        [make_record(term_months_normalised=0, term_label_raw="0 months")]
    )

    assert outcome.rejected
    assert outcome.errors[0].code == "invalid_term"


def test_a_missing_source_is_rejected(make_record):
    record = make_record().model_copy(update={"source_url": ""})
    outcome = validate_observations([record])

    assert outcome.rejected
    assert {issue.code for issue in outcome.errors} == {"missing_source"}


def test_a_missing_term_is_rejected(make_record):
    record = make_record().model_copy(
        update={"term_months_normalised": None, "term_label_raw": ""}
    )
    outcome = validate_observations([record])

    assert outcome.rejected
    assert "missing_term" in {issue.code for issue in outcome.errors}


def test_a_suspicious_jump_is_flagged_but_kept(make_record):
    """A genuine large reprice must not be silently discarded."""
    previous = [make_record(rate_pct=4.89)]
    outcome = validate_observations(
        [make_record(rate_pct=8.49)],
        thresholds=ValidationThresholds(suspicious_change_bps=75),
        previous=previous,
    )

    assert len(outcome.accepted) == 1, "kept for review, not dropped"
    assert outcome.rejected == []
    assert len(outcome.warnings) == 1
    assert outcome.warnings[0].code == "suspicious_movement"
    assert outcome.suspicious_keys


def test_a_suspicious_movement_is_caught_across_an_eligibility_edit(make_record):
    """An eligibility edit changes the exact key; the movement check must not skip it."""
    previous = [make_record(max_lvr=0.80, min_equity=0.20, rate_pct=5.00)]
    outcome = validate_observations(
        [make_record(max_lvr=0.75, min_equity=0.25, rate_pct=1.50)],
        thresholds=ValidationThresholds(suspicious_change_bps=75),
        previous=previous,
    )

    assert len(outcome.accepted) == 1
    assert outcome.suspicious_keys, "the movement must be caught via the eligibility fallback"


def test_a_normal_movement_is_not_flagged(make_record):
    outcome = validate_observations(
        [make_record(rate_pct=4.79)],
        thresholds=ValidationThresholds(suspicious_change_bps=75),
        previous=[make_record(rate_pct=4.99)],
    )

    assert outcome.warnings == []


def test_the_suspicious_threshold_is_configurable(make_record):
    previous = [make_record(rate_pct=4.99)]
    strict = validate_observations(
        [make_record(rate_pct=4.79)],
        thresholds=ValidationThresholds(suspicious_change_bps=10),
        previous=previous,
    )
    lenient = validate_observations(
        [make_record(rate_pct=4.79)],
        thresholds=ValidationThresholds(suspicious_change_bps=100),
        previous=previous,
    )

    assert strict.warnings and not lenient.warnings


def test_a_bad_record_does_not_reject_the_good_ones(make_record):
    outcome = validate_observations(
        [make_record(bank="ANZ", rate_pct=4.79), make_record(bank="BNZ", rate_pct=99.0)]
    )

    assert [record.bank for record in outcome.accepted] == ["ANZ"]
    assert [record.bank for record in outcome.rejected] == ["BNZ"]


def test_deposit_rates_use_the_deposit_band(make_record):
    thresholds = ValidationThresholds(min_mortgage_rate_pct=3.0, min_deposit_rate_pct=0.0)
    record = make_record(rate_pct=0.5, product_type="term_deposit")

    assert validate_observations([record], thresholds).accepted
