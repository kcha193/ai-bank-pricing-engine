"""Tests for the additive governance decision-record artifact."""

import json
from types import SimpleNamespace

from balance_pricing_engine.features.contracts import FeatureLineage
from balance_pricing_engine.governance.decision_record import (
    build_decision_record,
    write_decision_record,
)
from balance_pricing_engine.governance.gates import GateResult, GateStatus
from balance_pricing_engine.governance.lineage import synthetic_model_lineage


def _diagnostics():
    return SimpleNamespace(
        best_result=SimpleNamespace(
            loan_rate=0.08,
            deposit_rate=0.05,
            total_objective=1_000_000.0,
            expected_loan_volume=100_000_000.0,
            expected_deposit_volume=80_000_000.0,
            funding_shortfall=0.0,
        ),
        boundary_diagnostics=SimpleNamespace(has_warning=False),
        initial_boundary_diagnostics=SimpleNamespace(has_warning=False),
        expansion_rounds=0,
        expansion_capped=False,
        policy_band_breached=False,
    )


def _feature_lineage() -> dict:
    return {
        "loan": FeatureLineage(
            feature_set_name="loan_response_features",
            feature_set_version="v1",
            schema_fingerprint="abc123",
            product="loan",
            as_of="2026-07-13T00:00:00+00:00",
            row_count=100,
            source="synthetic_local_store",
        )
    }


def test_build_decision_record_reflects_diagnostics_and_status():
    record = build_decision_record(
        decision_id="dec-1",
        correlation_id="corr-1",
        scenario_name="baseline",
        timestamp="2026-07-13T00:00:00+00:00",
        decision_status="RECOMMENDED",
        config_hash="hash123",
        package_version="0.1.0",
        git_commit="abc1234",
        diagnostics=_diagnostics(),
        model_lineage={"loan": synthetic_model_lineage("loan")},
        feature_lineage=_feature_lineage(),
        governance_gates=[
            GateResult(code="OBJECTIVE_FINITE", status=GateStatus.PASS, message="ok", severity="info")
        ],
        execution_duration_seconds=1.23,
    )

    assert record.decision_id == "dec-1"
    assert record.best_loan_rate == 0.08
    assert record.decision_status == "RECOMMENDED"
    assert record.used_deterministic_response is True
    assert record.model_lineage["loan"]["response_source"] == "deterministic_synthetic"
    assert record.governance_gates[0]["code"] == "OBJECTIVE_FINITE"


def test_build_decision_record_flags_non_deterministic_response():
    from balance_pricing_engine.governance.lineage import ModelLineage

    approved_lineage = ModelLineage(
        product="loan",
        response_source="approved_ml_champion",
        model_name="LogisticResponseModel",
        model_version="v1",
        governance_status="approved",
        champion_status="champion",
        artifact_path="/tmp/model.joblib",
        used_fallback=False,
    )

    record = build_decision_record(
        decision_id="dec-2",
        correlation_id="corr-1",
        scenario_name="baseline",
        timestamp="2026-07-13T00:00:00+00:00",
        decision_status="RECOMMENDED",
        config_hash="hash123",
        package_version="0.1.0",
        git_commit=None,
        diagnostics=_diagnostics(),
        model_lineage={"loan": approved_lineage, "deposit": synthetic_model_lineage("deposit")},
        feature_lineage=_feature_lineage(),
        governance_gates=[],
        execution_duration_seconds=1.0,
    )

    assert record.used_deterministic_response is False


def test_write_decision_record_writes_additive_json_file(tmp_path):
    record = build_decision_record(
        decision_id="dec-1",
        correlation_id="corr-1",
        scenario_name="baseline",
        timestamp="2026-07-13T00:00:00+00:00",
        decision_status="RECOMMENDED",
        config_hash="hash123",
        package_version="0.1.0",
        git_commit=None,
        diagnostics=_diagnostics(),
        model_lineage={"loan": synthetic_model_lineage("loan")},
        feature_lineage=_feature_lineage(),
        governance_gates=[],
        execution_duration_seconds=0.5,
    )
    # Simulate an existing run-ledger directory with unrelated files already present.
    (tmp_path / "summary.json").write_text("{}", encoding="utf-8")

    path = write_decision_record(tmp_path, record)

    assert path == tmp_path / "decision_record.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["decision_id"] == "dec-1"
    # Existing artifact untouched.
    assert (tmp_path / "summary.json").read_text(encoding="utf-8") == "{}"
