"""Tests for governance gate evaluation and decision-status derivation."""

from types import SimpleNamespace

import pytest

from balance_pricing_engine.competitor_rates.models import ValidationIssue
from balance_pricing_engine.config import load_config
from balance_pricing_engine.features.contracts import FeatureLineage
from balance_pricing_engine.governance.gates import (
    DecisionStatus,
    GateStatus,
    decision_status_from_gates,
    evaluate_gates,
)
from balance_pricing_engine.governance.lineage import ModelLineage, synthetic_model_lineage


def _clean_diagnostics(**overrides):
    defaults = dict(
        best_result=SimpleNamespace(funding_shortfall=0.0, total_objective=1_000_000.0),
        boundary_diagnostics=SimpleNamespace(has_warning=False, warnings=()),
        expansion_capped=False,
        expansion_rounds=0,
        policy_band_breached=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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
        ),
        "deposit": FeatureLineage(
            feature_set_name="deposit_response_features",
            feature_set_version="v1",
            schema_fingerprint="def456",
            product="deposit",
            as_of="2026-07-13T00:00:00+00:00",
            row_count=100,
            source="synthetic_local_store",
        ),
    }


def _synthetic_model_lineage() -> dict:
    return {"loan": synthetic_model_lineage("loan"), "deposit": synthetic_model_lineage("deposit")}


def _config():
    return load_config("configs/base.yaml")


def test_clean_scenario_yields_all_pass_gates_and_recommended_status():
    gates = evaluate_gates(
        config=_config(),
        diagnostics=_clean_diagnostics(),
        model_lineage=_synthetic_model_lineage(),
        feature_lineage=_feature_lineage(),
        competitor_rate_issues=None,
    )

    assert all(gate.status is GateStatus.PASS for gate in gates)
    assert decision_status_from_gates(gates) is DecisionStatus.RECOMMENDED


def test_boundary_warning_yields_review_required():
    diagnostics = _clean_diagnostics(
        boundary_diagnostics=SimpleNamespace(has_warning=True, warnings=("at boundary",))
    )

    gates = evaluate_gates(
        config=_config(),
        diagnostics=diagnostics,
        model_lineage=_synthetic_model_lineage(),
        feature_lineage=_feature_lineage(),
        competitor_rate_issues=None,
    )

    boundary_gate = next(g for g in gates if g.code == "BOUNDARY_WARNING")
    assert boundary_gate.status is GateStatus.WARN
    assert decision_status_from_gates(gates) is DecisionStatus.REVIEW_REQUIRED


def test_funding_shortfall_yields_review_required_with_evidence():
    diagnostics = _clean_diagnostics(
        best_result=SimpleNamespace(funding_shortfall=5_000_000.0, total_objective=1_000_000.0)
    )

    gates = evaluate_gates(
        config=_config(),
        diagnostics=diagnostics,
        model_lineage=_synthetic_model_lineage(),
        feature_lineage=_feature_lineage(),
        competitor_rate_issues=None,
    )

    shortfall_gate = next(g for g in gates if g.code == "FUNDING_SHORTFALL")
    assert shortfall_gate.status is GateStatus.WARN
    assert shortfall_gate.evidence["funding_shortfall"] == 5_000_000.0
    assert decision_status_from_gates(gates) is DecisionStatus.REVIEW_REQUIRED


def test_non_finite_objective_blocks():
    diagnostics = _clean_diagnostics(
        best_result=SimpleNamespace(funding_shortfall=0.0, total_objective=float("nan"))
    )

    gates = evaluate_gates(
        config=_config(),
        diagnostics=diagnostics,
        model_lineage=_synthetic_model_lineage(),
        feature_lineage=_feature_lineage(),
        competitor_rate_issues=None,
    )

    objective_gate = next(g for g in gates if g.code == "OBJECTIVE_FINITE")
    assert objective_gate.status is GateStatus.BLOCK
    assert decision_status_from_gates(gates) is DecisionStatus.BLOCKED


def test_block_takes_priority_over_warn():
    diagnostics = _clean_diagnostics(
        best_result=SimpleNamespace(funding_shortfall=1.0, total_objective=float("inf")),
        boundary_diagnostics=SimpleNamespace(has_warning=True, warnings=("edge",)),
    )

    gates = evaluate_gates(
        config=_config(),
        diagnostics=diagnostics,
        model_lineage=_synthetic_model_lineage(),
        feature_lineage=_feature_lineage(),
        competitor_rate_issues=None,
    )

    assert decision_status_from_gates(gates) is DecisionStatus.BLOCKED


def test_competitor_rate_strict_error_blocks():
    config = _config()
    config = config.model_copy(
        update={"competitor_rates": config.competitor_rates.model_copy(update={"enabled": True})}
    )
    issues = [ValidationIssue(severity="error", message="competitor rate file missing")]

    gates = evaluate_gates(
        config=config,
        diagnostics=_clean_diagnostics(),
        model_lineage=_synthetic_model_lineage(),
        feature_lineage=_feature_lineage(),
        competitor_rate_issues=issues,
    )

    competitor_gate = next(g for g in gates if g.code == "COMPETITOR_RATE_FRESHNESS")
    assert competitor_gate.status is GateStatus.BLOCK
    assert decision_status_from_gates(gates) is DecisionStatus.BLOCKED


def test_competitor_rate_disabled_passes_without_issues():
    gates = evaluate_gates(
        config=_config(),
        diagnostics=_clean_diagnostics(),
        model_lineage=_synthetic_model_lineage(),
        feature_lineage=_feature_lineage(),
        competitor_rate_issues=None,
    )

    competitor_gate = next(g for g in gates if g.code == "COMPETITOR_RATE_FRESHNESS")
    assert competitor_gate.status is GateStatus.PASS


def test_approved_champion_model_gate_reports_champion_metadata():
    model_lineage = {
        "loan": ModelLineage(
            product="loan",
            response_source="approved_ml_champion",
            model_name="LogisticResponseModel",
            model_version="v1",
            governance_status="approved",
            champion_status="champion",
            artifact_path="/tmp/model.joblib",
            used_fallback=False,
        ),
        "deposit": synthetic_model_lineage("deposit"),
    }

    gates = evaluate_gates(
        config=_config(),
        diagnostics=_clean_diagnostics(),
        model_lineage=model_lineage,
        feature_lineage=_feature_lineage(),
        competitor_rate_issues=None,
    )

    loan_gate = next(g for g in gates if g.code == "MODEL_APPROVAL_STATUS[loan]")
    assert loan_gate.status is GateStatus.PASS
    assert loan_gate.evidence["model_name"] == "LogisticResponseModel"


def test_missing_lineage_warns_incomplete():
    gates = evaluate_gates(
        config=_config(),
        diagnostics=_clean_diagnostics(),
        model_lineage={"loan": synthetic_model_lineage("loan")},  # deposit missing
        feature_lineage=_feature_lineage(),
        competitor_rate_issues=None,
    )

    lineage_gate = next(g for g in gates if g.code == "LINEAGE_COMPLETE")
    assert lineage_gate.status is GateStatus.WARN
    assert decision_status_from_gates(gates) is DecisionStatus.REVIEW_REQUIRED


@pytest.mark.parametrize(
    "gates,expected",
    [
        ([], DecisionStatus.RECOMMENDED),
    ],
)
def test_empty_gate_list_is_recommended(gates, expected):
    assert decision_status_from_gates(gates) is expected
