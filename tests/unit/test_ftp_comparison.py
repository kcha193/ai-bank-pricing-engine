"""FIN-003 (Phase 4d): reproducible FTP comparison evidence."""

from copy import deepcopy
import json
import math
from pathlib import Path
import re

import pytest
import yaml

from examples.compare_ftp import BASE_CONFIG, build_report

ROOT = Path(__file__).resolve().parents[2]
COMPARISON_EVIDENCE = ROOT / "docs/audits/phase_4d_evidence/comparison.json"


@pytest.fixture(scope="module")
def report():
    return build_report()


def _evidence_location(path):
    return "$" + "".join(
        f".{part}" if isinstance(part, str) and part.isidentifier() else f"[{json.dumps(part)}]"
        for part in path
    )


def _measurement_abs_tolerance(path):
    # Linux shifted a NZ$792m sum by two ULPs (NZ$2.4e-7). The relative tolerance in
    # the comparator covers accumulation, and this floor covers cancellation near zero.
    amount_tolerance = 1e-7
    if path == ("checks", "max_abs_ftp_reconciliation_difference"):
        return amount_tolerance
    if path[:1] == ("canonical",):
        fields = path[1:]
    elif path[:1] == ("scenarios",) and path[2:3] in {("fixed_offer",), ("reoptimised",)}:
        fields = path[3:]
    else:
        return None

    if fields in {
        ("return_on_equity",),
        ("components", "O_model_roe"),
        ("components", "P_economic_roe"),
        ("economic_roe", "model_return_on_equity"),
        ("economic_roe", "economic_return_on_equity"),
        ("economic_roe", "steering_roe_difference"),
    }:
        return 1e-12  # ROE is dimensionless.
    if fields in {
        ("total_objective",),
        ("expected_loan_volume",),
        ("expected_deposit_volume",),
        ("funding_shortfall",),
        ("economic_roe", "capital_denominator", "loan_volume"),
        ("economic_roe", "capital_denominator", "required_capital"),
        ("economic_roe", "steering_penalty_difference"),
    } or fields[:1] in {("components",), ("deposit_supply",)}:
        return amount_tolerance
    if fields[:2] == ("economic_roe", "numerator_bridge"):
        return amount_tolerance
    if (
        fields[:1] == ("ledger",)
        and not fields[-1].endswith("_rate")
        and fields[-1] != "internal_transfer_net"
    ):
        return amount_tolerance
    return None


def _assert_evidence_matches(observed, recorded, path=()):
    location = _evidence_location(path)
    assert type(observed) is type(recorded), (
        f"{location}: expected {type(recorded).__name__}, got {type(observed).__name__}"
    )
    if isinstance(recorded, dict):
        missing = recorded.keys() - observed.keys()
        additional = observed.keys() - recorded.keys()
        assert not missing, f"{_evidence_location((*path, min(missing)))}: missing field"
        assert not additional, f"{_evidence_location((*path, min(additional)))}: unexpected field"
        for key in recorded:
            _assert_evidence_matches(observed[key], recorded[key], (*path, key))
    elif isinstance(recorded, list):
        assert len(observed) == len(recorded), f"{location}: expected {len(recorded)} entries"
        for index, (actual, expected) in enumerate(zip(observed, recorded)):
            _assert_evidence_matches(actual, expected, (*path, index))
    else:
        if isinstance(recorded, float):
            assert math.isfinite(observed) and math.isfinite(recorded), (
                f"{location}: expected finite evidence"
            )
            abs_tolerance = _measurement_abs_tolerance(path)
            if abs_tolerance is not None:
                assert math.isclose(observed, recorded, rel_tol=1e-14, abs_tol=abs_tolerance), (
                    f"{location}: {observed!r} != {recorded!r}"
                )
                return
        assert observed == recorded, f"{location}: {observed!r} != {recorded!r}"


def test_committed_evidence_reproduces(report):
    committed = json.loads(COMPARISON_EVIDENCE.read_text(encoding="utf-8"))
    _assert_evidence_matches(report, committed)


def test_evidence_accepts_observed_platform_rounding():
    committed = json.loads(COMPARISON_EVIDENCE.read_text(encoding="utf-8"))
    observed = deepcopy(committed)
    observed["scenarios"]["curve_inverted"]["reoptimised"]["expected_loan_volume"] = 792389411.4630415
    assert observed != committed
    _assert_evidence_matches(observed, committed)


@pytest.mark.parametrize(
    ("path", "change"),
    [
        (("canonical", "total_objective"), lambda value: value + 0.01),
        (("canonical", "loan_rate"), lambda value: math.nextafter(value, math.inf)),
        (("fixed_offer", 0), lambda value: math.nextafter(value, math.inf)),
        (
            ("scenarios", "scalar_equivalent", "reoptimised", "selected_deposit_rate"),
            lambda value: math.nextafter(value, math.inf),
        ),
        (("canonical", "config_hash"), lambda _: "different-hash"),
        (("canonical", "governance_status"), lambda _: "RECOMMENDED"),
        (("scenarios", "scalar_equivalent", "curve_key"), lambda _: "different-curve"),
        (("approval_status",), lambda _: "APPROVED"),
        (("canonical", "policy_band_breached"), int),
        (("canonical", "total_objective"), str),
        (("canonical", "total_objective"), lambda _: math.nan),
        (("canonical", "total_objective"), lambda _: math.inf),
    ],
)
def test_evidence_rejects_changed_values_and_types_at_their_path(path, change):
    committed = json.loads(COMPARISON_EVIDENCE.read_text(encoding="utf-8"))
    observed = deepcopy(committed)
    parent = observed
    for part in path[:-1]:
        parent = parent[part]
    key = path[-1]
    parent[key] = change(parent[key])
    with pytest.raises(AssertionError, match=re.escape(_evidence_location(path))):
        _assert_evidence_matches(observed, committed)


def test_evidence_rejects_missing_additional_and_renamed_fields():
    committed = {"scenarios": {"curve_inverted": {}}, "fixed_offer": [0.0619, 0.0405]}
    with pytest.raises(AssertionError, match=r"\$\.scenarios\.curve_inverted"):
        _assert_evidence_matches({**committed, "scenarios": {"inverted_curve": {}}}, committed)
    with pytest.raises(AssertionError, match=r"\$\.new_field"):
        _assert_evidence_matches({**committed, "new_field": None}, committed)
    with pytest.raises(AssertionError, match=r"\$\.fixed_offer"):
        _assert_evidence_matches({**committed, "fixed_offer": [0.0619]}, committed)


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


def test_negative_wholesale_spread_report_reproduces_scalar_pricing(tmp_path):
    payload = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
    payload["market"]["wholesale_funding_spread"] = -0.001
    config_path = tmp_path / "negative_spread.yaml"
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.warns(UserWarning, match="negative wholesale_funding_spread"):
        report = build_report(config_path)

    assert report["checks"]["scalar_equivalent_reproduces_canonical"] is True
    assert report["checks"]["every_run_requires_review"] is True
    equivalent = report["scenarios"]["scalar_equivalent"]["reoptimised"]
    ledger = equivalent["ledger"]
    wholesale_rate = payload["market"]["cash_rate"] - 0.001
    assert equivalent["rates"]["wholesale_funding_rate"] == wholesale_rate
    assert equivalent["rates"]["surplus_reinvestment_rate"] == payload["market"]["cash_rate"]
    for quote in equivalent["rates"]["quotes"]:
        assert quote["liquidity_premium"] == 0.0
        assert quote["adjustments"] == [["legacy_negative_wholesale_spread", -0.001]]
    assert ledger["wholesale_deficit"] > 0.0
    assert ledger["external_wholesale_funding_cost"] == ledger["wholesale_deficit"] * wholesale_rate
    assert ledger["internal_transfer_net"] == 0.0
    assert ledger["consolidated_contribution"] == pytest.approx(
        ledger["external_economic_contribution"], rel=0, abs=1e-7
    )
    assert equivalent["economic_roe"] == report["canonical"]["economic_roe"]


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
