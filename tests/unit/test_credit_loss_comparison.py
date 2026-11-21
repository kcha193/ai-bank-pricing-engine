"""Reproducibility and independent reconciliation for the Phase 4b comparison."""

import json
from pathlib import Path

import pytest
import yaml

from scripts.run_credit_loss_comparison import build_report, write_report

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "configs/base.yaml"
PHASE_4A = ROOT / "docs/audits/phase_4a_evidence/calibration-report.json"
PHASE_4B = ROOT / "docs/audits/phase_4b_evidence/comparison.json"


@pytest.fixture(scope="module")
def report():
    return build_report(BASE)


def test_reoptimised_legacy_economics_match_phase_4a(report):
    historical = json.loads(PHASE_4A.read_text(encoding="utf-8"))
    expected = historical["baseline"]
    actual = report["reoptimised"]["legacy_synthetic"]
    for key in (
        "loan_rate", "deposit_rate", "config_hash", "expansion_rounds", "policy_band_breached",
        "governance_status", "final_grid_size",
    ):
        assert actual[key] == expected[key]
    for key in (
        "total_objective", "expected_credit_loss", "expected_loan_volume",
        "expected_deposit_volume", "return_on_equity",
    ):
        assert actual[key] == pytest.approx(expected[key], rel=1e-12, abs=1e-6)
    assert report["source_config_file_sha256"] == historical["source_config_file_sha256"]
    assert report["matched_inputs"]["random_seed"] == historical["random_seed"]
    assert report["reoptimised_shared_final_grid"] is True


def test_fixed_offer_changes_only_loss_and_its_effects(report):
    fixed = report["fixed_offer"]
    legacy, alternate = fixed["legacy_synthetic"], fixed["pd_lgd_ead"]
    for component in (
        "loan_rate", "deposit_rate", "loan_interest_income", "capital_charge",
        "expected_loan_volume", "expected_deposit_volume", "deposit_interest_cost",
        "wholesale_funding_cost", "excess_deposit_income", "funding_shortfall",
        "funding_shortfall_penalty", "policy_band_breached",
    ):
        assert legacy[component] == alternate[component]
    assert legacy["governance_status"] == alternate["governance_status"] == "NOT_ASSESSED"
    assert report["alternate_simulation_governance_status"] == "REVIEW_REQUIRED"
    assert legacy["policy_band_breached"] is True
    assert legacy["loan_rate"] == report["reoptimised"]["legacy_synthetic"]["loan_rate"]
    assert legacy["deposit_rate"] == report["reoptimised"]["legacy_synthetic"]["deposit_rate"]
    delta = fixed["pd_lgd_ead_minus_legacy"]
    assert delta["total_objective"] == pytest.approx(-delta["expected_credit_loss"], abs=1e-6)


@pytest.mark.parametrize("mode", ["fixed_offer", "reoptimised"])
@pytest.mark.parametrize("model", ["legacy_synthetic", "pd_lgd_ead"])
def test_lvr_attribution_and_objective_reconcile(report, mode, model):
    comparison = report[mode][model]
    bands = comparison["loss"]["lvr_bands"]
    assert sum(band["prospect_count"] for band in bands) == 5000
    assert sum(band["expected_loan_volume"] for band in bands) == pytest.approx(
        comparison["expected_loan_volume"]
    )
    assert sum(band["expected_credit_loss"] for band in bands) == pytest.approx(
        comparison["expected_credit_loss"]
    )
    assert comparison["loss"]["booked_balance_weighted_annual_loss_rate"] == pytest.approx(
        comparison["expected_credit_loss"] / comparison["expected_loan_volume"]
    )
    expected = (
        comparison["loan_interest_income"]
        - comparison["expected_credit_loss"]
        - comparison["capital_charge"]
        - comparison["deposit_interest_cost"]
        - comparison["wholesale_funding_cost"]
        - comparison["funding_shortfall_penalty"]
        + comparison["excess_deposit_income"]
    )
    assert expected == pytest.approx(comparison["total_objective"], abs=1e-6)


def test_profile_lineage_and_governance(report):
    adapted = report["reoptimised"]["pd_lgd_ead"]
    legacy = report["reoptimised"]["legacy_synthetic"]
    assert adapted["governance_status"] == "REVIEW_REQUIRED"
    assert report["classification"] == "SYNTHETIC / UNCALIBRATED"
    assert report["empirically_validated"] is False
    assert report["approved_for_pricing"] is False
    assert report["profile"]["version"]
    assert adapted["config_hash"] != legacy["config_hash"]


def test_report_matches_recorded_evidence_and_preserves_source(report, tmp_path):
    before = BASE.read_bytes()
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    write_report(BASE, first, report)
    write_report(BASE, second, build_report(BASE))
    assert first.read_bytes() == second.read_bytes()
    recorded = json.loads(PHASE_4B.read_text(encoding="utf-8"))
    assert report["profile"] == recorded["profile"]
    for mode in ("fixed_offer", "reoptimised"):
        for model in ("legacy_synthetic", "pd_lgd_ead"):
            for key in ("loan_rate", "deposit_rate", "expected_credit_loss", "total_objective"):
                assert report[mode][model][key] == pytest.approx(
                    recorded[mode][model][key], rel=1e-12, abs=1e-6
                )
    alias = tmp_path / "source-link.json"
    alias.symlink_to(BASE)
    with pytest.raises(ValueError, match="source configuration"):
        write_report(BASE, alias, report)
    assert BASE.read_bytes() == before


@pytest.mark.parametrize("input_mode", ["competitor_rates", "absolute_grid"])
def test_disallows_unmatched_or_live_inputs(input_mode, tmp_path):
    payload = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    if input_mode == "competitor_rates":
        payload["competitor_rates"]["enabled"] = True
    else:
        payload.pop("pricing_strategy")
        payload["products"] = {
            "loan_rate_grid": {"start": 0.04, "stop": 0.05, "step": 0.01},
            "deposit_rate_grid": {"start": 0.03, "stop": 0.04, "step": 0.01},
        }
    source = tmp_path / "alternate.yaml"
    source.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="market-relative joint pricing and static rates"):
        build_report(source)
