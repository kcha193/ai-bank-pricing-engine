"""Validation stops a parser fault from becoming market data."""

import pytest

from balance_pricing_engine.pricing_intelligence.config import ValidationThresholds
from balance_pricing_engine.pricing_intelligence.validation import validate_observations


def test_a_plausible_observation_is_accepted(make_record):
    outcome = validate_observations([make_record(rate_pct=4.79)])

    assert len(outcome.accepted) == 1
    assert outcome.rejected == []
    assert outcome.errors == []


def test_strip_advertised_rate_token_removes_only_one_matching_occurrence():
    """A structural percentage that coincidentally equals the advertised
    rate (e.g. an allocation percentage) must survive -- stripping every
    matching occurrence would erase that structural qualifier entirely
    instead of just the rate's own token, the same failure mode
    changes._normalized_special_conditions() already guards against for an
    analogous case."""
    from balance_pricing_engine.pricing_intelligence.validation import (
        _strip_advertised_rate_token,
    )

    stripped = _strip_advertised_rate_token("Growth Fund | 4% allocation | 4% return", 4.0)

    assert stripped.count("%") == 1


@pytest.mark.parametrize("rate", [0.1, 19.99, -1.0, 0.0])
def test_an_implausible_rate_is_rejected(make_record, rate):
    outcome = validate_observations([make_record(rate_pct=rate)])

    assert outcome.accepted == []
    assert len(outcome.rejected) == 1
    assert outcome.errors[0].code == "rate_out_of_range"


def test_conflicting_rates_for_the_same_identity_within_one_batch_are_rejected(make_record):
    """Two records claiming the same observation_key() with different rates
    in the same collection (e.g. a genuine 4.79% alongside a misparsed
    1.00% for the same bank/category/term) must not both pass. Each is only
    validated against its own predecessor, never against each other -- and
    on a bank's first-ever collection there is no predecessor at all, so
    the movement check can't catch this either. Without an explicit
    within-batch check, both would reach storage, where insertion order
    (not plausibility) decides which becomes "current"."""
    genuine = make_record(rate_pct=4.79)
    misparsed = make_record(rate_pct=1.00)

    outcome = validate_observations([genuine, misparsed])

    assert outcome.accepted == []
    assert len(outcome.rejected) == 2
    assert {issue.code for issue in outcome.errors} == {"conflicting_observation"}


def test_repeating_the_same_rate_within_one_batch_is_not_a_conflict(make_record):
    """An exact duplicate reading (same key, same rate) is not ambiguous --
    only *different* rates for the same identity are a conflict."""
    first = make_record(rate_pct=4.79)
    second = make_record(rate_pct=4.79)

    outcome = validate_observations([first, second])

    assert len(outcome.accepted) == 2
    assert outcome.rejected == []


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


def test_a_suspicious_movement_is_caught_across_a_rate_category_flip(make_record):
    """A parser fault can flip rate_category alongside a misread rate: this has no
    predecessor under observation_key or eligibility_group_key (both include
    rate_category), so only the category-agnostic fallback catches the movement."""
    previous = [make_record(rate_category="special", rate_pct=5.00)]
    outcome = validate_observations(
        [make_record(rate_category="standard", rate_pct=1.50)],
        thresholds=ValidationThresholds(suspicious_change_bps=75),
        previous=previous,
    )

    assert len(outcome.accepted) == 1
    assert outcome.suspicious_keys, "the movement must be caught via the category-agnostic fallback"


def test_the_category_agnostic_fallback_survives_a_realistic_product_name_change(make_record):
    """The generic extractor derives product_name by searching the row for the
    category text itself (extractors._product_name), so a genuine category
    flip commonly changes product_name too (e.g. "Fixed Standard" -> "Fixed
    Special") -- category_agnostic_group_key() must not include it, or this
    realistic case defeats the very fallback it exists for."""
    previous = [make_record(rate_category="special", product_name="Fixed Special", rate_pct=5.00)]
    outcome = validate_observations(
        [make_record(rate_category="standard", product_name="Fixed Standard", rate_pct=1.50)],
        thresholds=ValidationThresholds(suspicious_change_bps=75),
        previous=previous,
    )

    assert len(outcome.accepted) == 1
    assert outcome.suspicious_keys, "the movement must be caught despite the product_name change"


def test_the_category_agnostic_fallback_is_not_used_when_ambiguous(make_record):
    """Two unclaimed predecessors sharing a category-agnostic group must not be
    guessed between -- an ambiguous match is not a safe baseline."""
    previous = [
        make_record(rate_category="special", rate_pct=5.00),
        make_record(rate_category="premier", rate_pct=5.10),
    ]
    outcome = validate_observations(
        [make_record(rate_category="standard", rate_pct=1.50)],
        thresholds=ValidationThresholds(suspicious_change_bps=75),
        previous=previous,
    )

    assert len(outcome.accepted) == 1
    assert not outcome.suspicious_keys, "an ambiguous fallback must not be used as a baseline"


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
