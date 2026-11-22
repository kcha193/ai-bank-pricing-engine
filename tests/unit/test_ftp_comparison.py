"""FIN-003 (Phase 4d): reproducible FTP comparison evidence."""

import json
from pathlib import Path

import pytest

from examples.compare_ftp import build_report

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def report():
    return build_report()


def test_committed_evidence_reproduces(report):
    committed = json.loads(
        (ROOT / "docs/audits/phase_4d_evidence/comparison.json").read_text(encoding="utf-8")
    )
    assert json.loads(json.dumps(report)) == committed


def test_canonical_phase_4a_baseline_is_preserved(report):
    archived = json.loads(
        (ROOT / "docs/audits/phase_4a_evidence/calibration-report.json").read_text(encoding="utf-8")
    )["baseline"]
    canonical = report["canonical"]
    for field in ("loan_rate", "deposit_rate", "governance_status", "policy_band_breached"):
        assert canonical[field] == archived[field]
    for field in ("total_objective", "return_on_equity"):
        assert canonical[field] == pytest.approx(archived[field], rel=1e-12)
    assert report["source_config_file_sha256"] == archived.get(
        "source_config_file_sha256", report["source_config_file_sha256"]
    )


def test_checks_and_review_status(report):
    checks = report["checks"]
    assert checks["scalar_equivalent_reproduces_canonical"] is True
    assert checks["max_abs_internal_transfer_net"] == 0.0
    assert checks["max_abs_ftp_reconciliation_difference"] < 1e-6
    assert checks["every_run_requires_review"] is True
    assert report["approval_status"] == "NOT APPROVED FOR PRICING"


def test_attribution_only_tenors_leave_consolidated_economics_unchanged(report):
    scenarios = report["scenarios"]
    base = scenarios["term_matched_12m"]["fixed_offer"]["components"]
    for name in ("repricing_tenor_24m", "loan_liquidity_60m"):
        components = scenarios[name]["fixed_offer"]["components"]
        assert (
            components["N_total_consolidated_objective"] == base["N_total_consolidated_objective"]
        )
        assert components["H_ftp_loan_allocation"] != base["H_ftp_loan_allocation"]
    external = scenarios["wholesale_tenor_36m"]["fixed_offer"]["components"]
    assert (
        external["F_external_wholesale_funding_expense"]
        > base["F_external_wholesale_funding_expense"]
    )


def test_phase_4c_supply_row_matches_phase_4c_evidence(report):
    # The 12-month illustrative wholesale rate equals the legacy 3.70%, so the Phase 4c
    # base profile must reproduce its own published fixed-offer and reoptimised results.
    supply = json.loads(
        (ROOT / "docs/audits/phase_4c_evidence/comparison.json").read_text(encoding="utf-8")
    )["scenarios"]["fixed_market_base"]
    row = report["scenarios"]["phase_4c_supply"]
    for mode in ("fixed_offer", "reoptimised"):
        ours, theirs = row[mode], supply[mode]
        assert ours["eligible_funding_basis"] == "phase_4c_recognised_incremental_funding"
        assert (ours["selected_loan_rate"], ours["selected_deposit_rate"]) == (
            theirs["loan_rate"],
            theirs["deposit_rate"],
        )
        assert ours["components"]["N_total_consolidated_objective"] == pytest.approx(
            theirs["total_objective"], rel=1e-9
        )
        assert ours["components"]["F_external_wholesale_funding_expense"] == pytest.approx(
            theirs["wholesale_funding_cost"], rel=1e-9
        )
