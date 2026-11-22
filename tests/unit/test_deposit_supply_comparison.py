"""Matched-offer and sampling evidence for the bounded supply experiment."""

import json
from pathlib import Path

import pytest

from examples.compare_deposit_supply import build_report, illustrative_profiles


@pytest.fixture(scope="module")
def report():
    return build_report()


def test_legacy_comparison_preserves_phase_4a_result(report):
    evidence = Path(__file__).resolve().parents[2] / "docs/audits/phase_4a_evidence"
    historical = json.loads((evidence / "calibration-report.json").read_text(encoding="utf-8"))
    archived = historical["baseline"]
    legacy = report["legacy_adaptive"]
    assert report["source_config_file_sha256"] == historical["source_config_file_sha256"]
    for field in ("loan_rate", "deposit_rate", "governance_status", "policy_band_breached"):
        assert legacy[field] == archived[field]
        for mode in ("fixed_offer", "reoptimised"):
            assert report["scenarios"]["legacy"][mode][field] == archived[field]
    for field in ("expected_deposit_volume", "total_objective", "return_on_equity"):
        assert legacy[field] == pytest.approx(archived[field], rel=1e-12)


def test_profiles_are_distinct_and_uncalibrated():
    profiles = illustrative_profiles()
    assert set(profiles) == {
        "fixed_market_base", "market_opportunity_low", "market_opportunity_high",
        "reachable_share_low", "reachable_share_high", "conversion_low", "conversion_high",
        "renewal_high", "cannibalisation_high", "concentration_cap",
    }
    assert {profile.profile_id for profile in profiles.values()} == set(profiles)
    assert all(profile.version for profile in profiles.values())
    assert all(profile.horizon_months == 12 for profile in profiles.values())


def test_sampling_changes_precision_without_changing_capacity(report):
    groups = report["sampling"]["independent_replicates"]
    assert [group["sample_count"] for group in groups] == [1000, 5000, 25000]
    assert len({group["eligible_opportunity"] for group in groups}) == 1
    base = report["scenarios"]["fixed_market_base"]
    assert report["scenarios"]["sample_5000"]["fixed_offer"] == base["fixed_offer"]
    for group in groups:
        assert len(group["observations"]) > 2
        assert group["fixed_offer_gross_bookings_sample_sd"] > 0
        capacity = group["eligible_opportunity"]
        assert all(
            0 < row["fixed_offer_gross_bookings"] <= capacity
            for row in group["observations"]
        )
    assert (
        groups[-1]["fixed_offer_gross_bookings_sample_sd"]
        < groups[0]["fixed_offer_gross_bookings_sample_sd"]
    )


def test_fixed_and_reoptimised_scenarios_share_controls(report):
    rows = report["scenarios"]
    fixed = rows["legacy"]["fixed_offer"]
    common_size = report["matched_inputs"]["reoptimised_candidate_pairs"]
    for case, comparison in rows.items():
        row = comparison["fixed_offer"]
        assert row["loan_rate"] == fixed["loan_rate"]
        assert row["deposit_rate"] == fixed["deposit_rate"]
        assert row["expected_credit_loss"] == fixed["expected_credit_loss"]
        assert row["expected_loan_volume"] == fixed["expected_loan_volume"]
        assert row["governance_status"] == "REVIEW_REQUIRED"
        assert comparison["reoptimised"]["candidate_pairs"] == common_size
        if not case.startswith("legacy"):
            assert row["gross_bookings"] <= row["eligible_opportunity"]
            assert row["eligible_incremental_funding"] <= row["net_incremental_funding"]
            assert "DEPOSIT_SUPPLY_SIMULATION_ONLY" in row["nonpassing_gates"]
    constrained = rows["concentration_cap"]["fixed_offer"]
    assert constrained["constrained_funding"] > 0
    assert rows["funding_ratio_stress"]["fixed_offer"]["funding_shortfall"] > 0
    assert report["empirically_validated"] is False
    assert report["approved_for_pricing"] is False
    json.dumps(report, allow_nan=False)


def test_legacy_capacity_expands_with_count_and_supply_does_not(report):
    cases = report["scenarios"]
    small = cases["legacy_sample_1000"]["fixed_offer"]
    large = cases["legacy_sample_25000"]["fixed_offer"]
    assert large["eligible_opportunity"] > 20 * small["eligible_opportunity"]
    assert large["gross_bookings"] > 20 * small["gross_bookings"]
    assert large["net_incremental_funding"] is None
    assert large["usable_funding"] == large["gross_bookings"]
    assert (
        cases["sample_25000"]["fixed_offer"]["eligible_opportunity"]
        == cases["sample_1000"]["fixed_offer"]["eligible_opportunity"]
    )


def test_funding_value_and_management_penalty_reconcile_separately(report):
    row = report["scenarios"]["concentration_cap"]["fixed_offer"]
    market = report["matched_inputs"]["market"]
    # Funding value is measured against no incremental funding; the opening book is excluded.
    extra_usable = row["usable_funding"]
    assert row["usable_funding"] < row["active_funding"] < row["expected_loan_volume"]
    assert row["economic_funding_value"] == pytest.approx(
        extra_usable * (market["cash_rate"] + market["wholesale_funding_spread"])
    )
    control = report["scenarios"]["fixed_market_base"]["fixed_offer"]
    policy = report["matched_inputs"]["balance_sheet"]
    for case in (row, control):
        # Both stay below the requirement, so each recognised NZD relieves the penalty once.
        required = case["expected_loan_volume"] * policy["required_deposit_funding_ratio"]
        assert case["usable_funding"] < required
        assert case["shortfall_penalty_avoided"] == pytest.approx(
            case["usable_funding"] * policy["funding_shortfall_penalty_rate"]
        )
    assert row["shortfall_penalty_avoided"] < control["shortfall_penalty_avoided"]
    assert row["gross_interest_cost"] == control["gross_interest_cost"]
    assert row["concentration_comparison"]["objective_delta"] < 0
