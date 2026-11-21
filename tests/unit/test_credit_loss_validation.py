"""Independent arithmetic checks on illustrative facility outcomes."""

import json

import pytest
from pydantic import ValidationError

from balance_pricing_engine.pricing.credit_loss_validation import (
    CreditLossOutcome,
    assess_credit_loss,
    compare_population_mix,
)


def _outcome(facility_id, **changes):
    return CreditLossOutcome(
        **{
            "facility_id": facility_id,
            "vintage": "2024",
            "lvr_band": "low",
            "segment": "owner_occupier",
            "booked_principal_nzd": 100,
            "predicted_pd": 0.1,
            "predicted_lgd": 0.25,
            "predicted_ead_nzd": 80,
            "predicted_annual_loss_nzd": 2,
            **changes,
        }
    )


@pytest.fixture
def outcomes():
    return [
        _outcome(
            "A",
            defaulted_12m=True,
            workout_resolved=True,
            booked_principal_nzd=120,
            balance_at_default_nzd=100,
            recovered_amount_nzd=80,
            workout_cost_nzd=10,
            stressed_annual_loss_nzd=4,
        ),
        _outcome(
            "B",
            defaulted_12m=False,
            predicted_pd=0.8,
            predicted_lgd=0.5,
            predicted_ead_nzd=110,
            predicted_annual_loss_nzd=44,
            stressed_annual_loss_nzd=60,
            lvr_band="high",
            segment="investor",
        ),
        _outcome(
            "C",
            defaulted_12m=True,
            booked_principal_nzd=140,
            predicted_pd=0.2,
            predicted_ead_nzd=150,
            predicted_annual_loss_nzd=15,
            balance_at_default_nzd=160,
            recovered_amount_nzd=60,
            vintage="2025",
            sample="holdout",
            lvr_band="high",
            segment="investor",
        ),
        _outcome(
            "D",
            defaulted_12m=True,
            workout_resolved=True,
            booked_principal_nzd=90,
            predicted_pd=0.4,
            predicted_ead_nzd=100,
            predicted_annual_loss_nzd=20,
            balance_at_default_nzd=90,
            workout_cost_nzd=5,
            vintage="2025",
            sample="holdout",
            lvr_band="unknown",
        ),
        _outcome("E", vintage="2025", sample="holdout", predicted_pd=0.9),
    ]


def test_pd_diagnostics_detect_poor_fit_after_excluding_censored_outcome(outcomes):
    metric = assess_credit_loss(outcomes)["overall"]["pd"]
    assert metric["observed_facilities"] == 4
    assert metric["unobserved_facilities"] == 1
    assert metric["observed_principal_nzd"] == 450
    assert metric["observed_defaults"] == 3
    assert metric["expected_defaults"] == pytest.approx(1.5)
    assert metric["observed_default_rate"] == 0.75
    assert metric["predicted_default_rate"] == pytest.approx(0.375)
    assert metric["observed_rate_wilson_95"] == pytest.approx([0.3006418, 0.9544127])
    assert metric["brier_score"] == pytest.approx(0.6125)
    assert metric["roc_auc"] == 0.0


def test_grouped_metrics_disclose_sparse_and_single_class_samples(outcomes):
    report = assess_credit_loss(outcomes)
    low = report["by_lvr_band"]["low"]["pd"]
    assert low["observed_defaults"] == low["observed_facilities"] == 1
    assert low["observed_rate_wilson_95"] == pytest.approx([0.2065493, 1])
    assert low["roc_auc"] is None
    assert report["by_segment"]["investor"]["pd"]["observed_facilities"] == 2
    assert report["by_vintage"]["2025"]["pd"]["observed_defaults"] == 2
    holdout = report["by_sample_and_vintage"]["holdout"]["2025"]
    assert holdout["ecl"]["matched_facilities"] == 0
    assert holdout["ecl"]["realised_loss_nzd"] is None


def test_lgd_reconciles_signed_net_loss_and_discloses_missing_recoveries(outcomes):
    metric = assess_credit_loss(outcomes)["overall"]["lgd"]
    assert metric["resolved_defaults"] == 2
    assert metric["unresolved_defaults"] == 1
    assert metric["resolved_missing_cash_flows"] == 1
    assert metric["severity_facilities"] == 1
    assert metric["severity_ead_nzd"] == 100
    assert metric["recoveries_nzd"] == 80
    assert metric["workout_costs_nzd"] == 10
    assert metric["net_loss_nzd"] == 30
    assert metric["observed_lgd"] == 0.30
    assert metric["predicted_lgd"] == 0.25
    assert metric["severity_bias"] == pytest.approx(-0.05)
    assert metric["mean_severity_error_standard_error"] is None


def test_ead_matches_observed_exposures_at_defaults_even_with_open_workouts(outcomes):
    outcomes.append(_outcome("F", defaulted_12m=True))
    metric = assess_credit_loss(outcomes)["overall"]["ead"]
    assert metric["defaults_with_ead"] == 3
    assert metric["defaults_missing_ead"] == 1
    assert metric["booked_principal_nzd"] == 350
    assert metric["observed_ead_nzd"] == 350
    assert metric["predicted_ead_nzd"] == 330
    assert metric["observed_minus_booked_nzd"] == 0
    assert metric["predicted_minus_observed_nzd"] == -20
    assert metric["mean_absolute_exposure_error_nzd"] == pytest.approx(40 / 3)


def test_ecl_compares_only_matched_outcomes_and_stress_uses_same_population(outcomes):
    metric = assess_credit_loss(outcomes)["overall"]
    assert metric["ecl"]["matched_facilities"] == 2
    assert metric["ecl"]["unmatched_facilities"] == 3
    assert metric["ecl"]["matched_principal_nzd"] == 220
    assert metric["ecl"]["predicted_loss_nzd"] == 46
    assert metric["ecl"]["realised_loss_nzd"] == 30
    assert metric["ecl"]["predicted_minus_realised_nzd"] == 16
    assert metric["stress"] == {
        "matched_facilities": 2,
        "matched_principal_nzd": 220,
        "baseline_loss_nzd": 46,
        "stressed_loss_nzd": 64,
        "loss_change_nzd": 18,
    }


def test_zero_ead_has_no_severity_denominator_and_signed_loss_is_retained():
    rows = [
        _outcome(
            "A",
            defaulted_12m=True,
            workout_resolved=True,
            balance_at_default_nzd=0,
            recovered_amount_nzd=0,
            workout_cost_nzd=3,
        ),
        _outcome(
            "B",
            defaulted_12m=True,
            workout_resolved=True,
            balance_at_default_nzd=100,
            recovered_amount_nzd=120,
            workout_cost_nzd=1,
        ),
    ]
    report = assess_credit_loss(rows)["overall"]
    assert report["lgd"]["resolved_zero_ead"] == 1
    assert report["lgd"]["observed_lgd"] == -0.19
    assert report["ecl"]["realised_loss_nzd"] == -16


def test_severity_uncertainty_uses_both_resolved_observations():
    rows = [
        _outcome(
            "A",
            defaulted_12m=True,
            workout_resolved=True,
            balance_at_default_nzd=100,
            recovered_amount_nzd=75,
            workout_cost_nzd=0,
        ),
        _outcome(
            "B",
            defaulted_12m=True,
            workout_resolved=True,
            balance_at_default_nzd=300,
            recovered_amount_nzd=165,
            workout_cost_nzd=0,
        ),
    ]
    metric = assess_credit_loss(rows)["overall"]["lgd"]
    assert metric["observed_lgd"] == 0.40
    assert metric["severity_bias"] == pytest.approx(-0.15)
    assert metric["mean_severity_error_standard_error"] == pytest.approx(0.10)


def test_population_drift_reports_unshared_categories_and_separate_balance_shares(outcomes):
    report = compare_population_mix(outcomes[:2], outcomes[2:])
    low = report["by_lvr_band"]["low"]
    unknown = report["by_lvr_band"]["unknown"]
    assert low["reference_count_share"] == 0.5
    assert low["evaluation_count_share"] == pytest.approx(1 / 3)
    assert low["principal_share_change"] == pytest.approx(100 / 330 - 120 / 220)
    assert unknown["reference_principal_share"] == 0
    assert unknown["evaluation_principal_share"] == pytest.approx(90 / 330)
    assert report["by_segment"]["investor"]["count_share_change"] == pytest.approx(1 / 3 - 0.5)


def test_empty_and_zero_principal_populations_serialize_without_nan():
    empty = assess_credit_loss([])["overall"]
    assert empty["pd"]["observed_facilities"] == 0
    assert empty["pd"]["observed_rate_wilson_95"] is None
    assert empty["lgd"]["observed_lgd"] is None
    assert empty["ead"]["observed_ead_nzd"] is None
    assert empty["stress"]["baseline_loss_nzd"] is None
    report = compare_population_mix([], [_outcome("A", booked_principal_nzd=0)])
    assert report["by_lvr_band"]["low"]["principal_share_change"] is None
    json.dumps([empty, report], allow_nan=False)


@pytest.mark.parametrize(
    "changes",
    [
        {"predicted_pd": 1.01},
        {"predicted_pd": float("nan")},
        {"predicted_lgd": -0.1},
        {"predicted_ead_nzd": float("inf")},
        {"booked_principal_nzd": -1},
        {"predicted_annual_loss_nzd": -1},
        {"defaulted_12m": True, "balance_at_default_nzd": -1},
        {"defaulted_12m": True, "recovered_amount_nzd": float("inf")},
        {"defaulted_12m": True, "workout_cost_nzd": -1},
        {"stressed_annual_loss_nzd": -1},
        {"facility_id": "  "},
        {"defaulted_12m": "unknown"},
        {"defaulted_12m": False, "recovered_amount_nzd": 1},
        {"workout_resolved": True},
    ],
)
def test_outcome_validation_rejects_invalid_or_inconsistent_inputs(changes):
    fields = {"facility_id": "X", **changes}
    with pytest.raises(ValidationError):
        _outcome(fields.pop("facility_id"), **fields)


@pytest.mark.parametrize("assess", [assess_credit_loss, compare_population_mix])
def test_duplicate_facilities_are_rejected(assess):
    duplicate = [_outcome("A"), _outcome("A", vintage="2025")]
    args = (duplicate, []) if assess is compare_population_mix else (duplicate,)
    with pytest.raises(ValueError, match="facility_id"):
        assess(*args)


def test_validation_revalidates_unchecked_pydantic_updates():
    corrupted = _outcome("A").model_copy(update={"predicted_pd": -1})
    with pytest.raises(ValidationError):
        assess_credit_loss([corrupted])
