"""Reproducibility and financial checks for the offline Phase 4A analysis."""

import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from balance_pricing_engine.application import PricingScenarioRequest, PricingService
from balance_pricing_engine.config import load_config
from balance_pricing_engine.config_identity import effective_config_hash
from scripts.run_calibration_baseline import Case, build_report, case_config, write_report

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "configs/base.yaml"
EVIDENCE = ROOT / "docs/audits/phase_4a_evidence"


@pytest.fixture(scope="module")
def report():
    return build_report(BASE)


def test_canonical_baseline_matches_governed_engine_and_source_hash(report):
    canonical = load_config(BASE)
    expected = PricingService().run_scenario(PricingScenarioRequest(canonical))
    baseline = report["baseline"]
    assert baseline["loan_rate"] == expected.best_loan_rate == 0.0619
    assert baseline["deposit_rate"] == expected.best_deposit_rate == 0.0405
    assert baseline["total_objective"] == expected.objective_value
    assert baseline["return_on_equity"] == expected.diagnostics.selected_return_on_equity
    assert baseline["config_hash"] == effective_config_hash(canonical)
    assert baseline["config_hash"] == expected.decision_record.config_hash
    assert report["source_config_file_sha256"] == hashlib.sha256(BASE.read_bytes()).hexdigest()
    assert report["random_seed"] == 42
    assert baseline["n_loan_customers"] == baseline["n_deposit_customers"] == 5000
    assert baseline["initial_grid_size"] == 357
    assert baseline["final_grid_size"] == len(expected.diagnostics.grid_results)
    assert baseline["governance_status"] == "REVIEW_REQUIRED"
    assert baseline["policy_band_breached"]
    assert baseline["expansion_rounds"] == 1
    assert baseline["initial_boundary_warning_flag"]
    assert not baseline["boundary_warning_flag"]
    assert baseline["perturbations"] == {}


def test_output_is_reproducible_and_preserves_source(report, tmp_path):
    original = BASE.read_bytes()
    result = tmp_path / "direct.json"
    write_report(BASE, result, report)
    cli_result = tmp_path / "cli.json"
    subprocess.run(
        [sys.executable, "-m", "scripts.run_calibration_baseline", "--output", str(cli_result)],
        cwd=ROOT,
        check=True,
    )
    assert result.read_bytes() == cli_result.read_bytes()
    assert json.loads(result.read_text(encoding="utf-8")) == report
    alias = tmp_path / "source-link.json"
    alias.symlink_to(BASE)
    with pytest.raises(ValueError, match="source configuration"):
        write_report(BASE, alias, report)
    assert BASE.read_bytes() == original


@pytest.mark.parametrize(
    "case",
    [
        Case("invalid", overrides=(("simulation.n_deposit_customers", 0),)),
        Case("invalid", overrides=(("balance_sheet.required_deposit_funding_ratio", 1.5),)),
        Case("invalid", overrides=(("market.wholesale_funding_spread", float("nan")),)),
        Case("invalid", overrides=(("market.nonexistent", 0.0),)),
        Case("invalid", overrides=(("competitor_rates.enabled", True),)),
        Case("invalid", overrides=(("random_seed", 7),)),
        Case("invalid", ecl_multiplier=0.5, overrides=(("simulation.n_deposit_customers", 10),)),
        Case("invalid", overrides=(("market.cash_rate", 0.01), ("market.cash_rate", 0.02))),
        Case("invalid", ecl_multiplier=0.0),
        Case("invalid", deposit_balance_multiplier=float("inf")),
        Case("invalid", fixed_rates=(0.03, -0.01)),
    ],
)
def test_invalid_cases_are_rejected(case):
    with pytest.raises(ValueError):
        case_config(load_config(BASE), case)


def test_cases_reconcile_objective_and_comparison(report):
    baseline = report["baseline"]
    cases = report["scenarios"]
    by_id = {row["scenario_id"]: row for row in cases}
    assert len(by_id) == len(cases)
    for row in [baseline, *cases]:
        objective = (
            row["loan_interest_income"]
            - row["expected_credit_loss"]
            - row["capital_charge"]
            - row["deposit_interest_cost"]
            - row["wholesale_funding_cost"]
            - row["funding_shortfall_penalty"]
            + row["excess_deposit_income"]
        )
        assert row["total_objective"] == pytest.approx(objective, rel=1e-12, abs=1e-6)
        assert row["return_on_equity"] == pytest.approx(
            (objective + row["capital_charge"]) / row["required_tier1_capital"]
        )
        assert row["delta_objective_nzd"] == pytest.approx(
            objective - baseline["total_objective"], rel=1e-12, abs=1e-6
        )
        assert row["delta_loan_rate_bps"] == pytest.approx(
            10_000 * (row["loan_rate"] - baseline["loan_rate"]), abs=1e-9
        )
        assert row["delta_deposit_rate_bps"] == pytest.approx(
            10_000 * (row["deposit_rate"] - baseline["deposit_rate"]), abs=1e-9
        )
        assert row["ecl_as_fraction_of_loan_volume"] == pytest.approx(
            row["expected_credit_loss"] / row["expected_loan_volume"]
        )
    assert by_id["prospects_10000"]["loan_rate"] == 0.0599
    assert by_id["prospects_25000"]["loan_rate"] == 0.0579
    assert by_id["funding_ratio_zero"]["loan_rate"] == 0.0564
    assert by_id["deposit_market_double"]["n_deposit_customers"] == 5000
    assert by_id["deposit_market_double"]["config_hash"] == baseline["config_hash"]
    assert by_id["ecl_half"]["config_hash"] == baseline["config_hash"]
    assert by_id["ecl_half"]["perturbations"] == {"ecl_multiplier": {"base": 1.0, "case": 0.5}}
    assert by_id["prospects_10000"]["config_hash"] != baseline["config_hash"]
    assert by_id["funding_ratio_zero"]["funding_shortfall"] == 0


def test_ecl_lvr_and_deposit_balance_decompositions(report):
    baseline = report["baseline"]
    ecl = report["ecl"]
    bands = ecl["lvr_bands"]
    assert sum(band["prospect_count"] for band in bands) == 5000
    assert sum(band["expected_loan_volume"] for band in bands) == pytest.approx(
        baseline["expected_loan_volume"]
    )
    assert sum(band["expected_credit_loss"] for band in bands) == pytest.approx(
        baseline["expected_credit_loss"]
    )
    assert ecl["booked_balance_weighted_ecl_rate"] == pytest.approx(
        baseline["ecl_as_fraction_of_loan_volume"]
    )
    assert report["deposit_supply"]["model_best"]["balance_weighted_conversion"] == pytest.approx(
        baseline["expected_deposit_volume"] / report["deposit_supply"]["opportunity_volume"]
    )


def test_interactions_compare_four_cells_and_fixed_offers(report):
    by_id = {row["scenario_id"]: row for row in [report["baseline"], *report["scenarios"]]}
    assert len(report["interactions"]) == 5
    for group in report["interactions"]:
        control, factor_a, factor_b, both = (by_id[key] for key in group["cell_ids"])
        assert len(set(group["cell_ids"])) == 4
        assert group["objective_interaction_nzd"] == pytest.approx(
            both["total_objective"] - factor_a["total_objective"]
            - factor_b["total_objective"] + control["total_objective"]
        )
        assert both["perturbations"].keys() == (
            factor_a["perturbations"].keys() | factor_b["perturbations"].keys()
        )
    fixed = [row for row in by_id.values() if row["mode"] == "fixed_offers"]
    assert fixed
    assert all(row["expansion_rounds"] is None for row in fixed)
    base_offers = by_id["fixed_base"]
    assert base_offers["total_objective"] == by_id["baseline"]["total_objective"]


def test_labels_and_evidence_do_not_imply_approval(report):
    assert report["classification"] == "SYNTHETIC / UNCALIBRATED"
    assert report["production_economic_calibration_applied"] is False
    assert report["calibration_approved"] is False
    assert report["baseline"]["assumption_source"] == "Canonical synthetic config and engine code"
    for row in report["scenarios"]:
        assert row["assumption_source"].startswith("Illustrative")
        assert row["governance_status"] in {"RECOMMENDED", "REVIEW_REQUIRED", "BLOCKED"}
    assert report["illustrative_ftp_curve"]["classification"] == "ILLUSTRATIVE SENSITIVITY ONLY"


def _assert_snapshot_value(observed, recorded, path="report"):
    if isinstance(recorded, dict):
        assert observed.keys() == recorded.keys(), path
        for key in recorded:
            _assert_snapshot_value(observed[key], recorded[key], f"{path}.{key}")
    elif isinstance(recorded, list):
        assert len(observed) == len(recorded), path
        for index, (current, previous) in enumerate(zip(observed, recorded)):
            _assert_snapshot_value(current, previous, f"{path}[{index}]")
    elif isinstance(recorded, float):
        assert observed == pytest.approx(recorded, rel=1e-12, abs=1e-7), path
    else:
        assert observed == recorded, path


def test_committed_report_matches_current_offline_regeneration(report):
    snapshot = json.loads((EVIDENCE / "calibration-report.json").read_text(encoding="utf-8"))
    _assert_snapshot_value(report, snapshot)


def test_config_only_case_has_no_undeclared_changes():
    base = load_config(BASE)
    original = base.model_dump()
    case = Case("example", overrides=(("balance_sheet.loan_rwa_weight", 0.45),))
    changed = case_config(base, case)
    assert base.model_dump() == original
    original["balance_sheet"]["loan_rwa_weight"] = 0.45
    assert changed.model_dump() == original


def test_evidence_matrix_has_complete_unapproved_rows():
    expected_columns = [
        "Assumption ID", "Economic parameter", "Current value", "Current source",
        "Proposed methodology", "Empirical data needed", "Public/private evidence classification",
        "Source date", "Data quality assessment", "Materiality", "Sensitivity result",
        "Validation owner", "Approval status", "Unresolved risk",
    ]
    with (EVIDENCE / "evidence-matrix.csv").open(encoding="utf-8", newline="") as handle:
        matrix = csv.DictReader(handle)
        assert matrix.fieldnames == expected_columns
        rows = list(matrix)
    assert len(rows) == len({row["Assumption ID"] for row in rows})
    assert {"FIN-001-ECL", "FIN-001-DEP-SIZE", "FIN-003-FTP", "FIN-004-RWA"} <= {
        row["Assumption ID"] for row in rows
    }
    for row in rows:
        assert None not in row and all(row.values())
        assert row["Approval status"] in {
            "EVIDENCE_AVAILABLE", "EVIDENCE_INCOMPLETE", "SME_VALIDATION_REQUIRED",
        }
