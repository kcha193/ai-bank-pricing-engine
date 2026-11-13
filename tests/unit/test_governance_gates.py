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


def _best_result(**overrides):
    defaults = dict(
        funding_shortfall=0.0,
        total_objective=1_000_000.0,
        expected_loan_volume=100_000_000.0,
        expected_deposit_volume=70_000_000.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _clean_diagnostics(**overrides):
    defaults = dict(
        best_result=_best_result(),
        boundary_diagnostics=SimpleNamespace(has_warning=False, warnings=()),
        expansion_capped=False,
        expansion_rounds=0,
        policy_band_breached=False,
        roe_constraint_relaxed=False,
        selected_return_on_equity=0.20,
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


# --- Phase 1a (OPT-001 / GOV-002): ROE floor, objective sign and volume gates ---


def _config_with_roe_floor(floor):
    config = _config()
    balance_sheet = config.balance_sheet.model_copy(update={"min_return_on_equity": floor})
    return config.model_copy(update={"balance_sheet": balance_sheet})


def _evaluate(config=None, **diagnostic_overrides):
    return evaluate_gates(
        config=config if config is not None else _config(),
        diagnostics=_clean_diagnostics(**diagnostic_overrides),
        model_lineage=_synthetic_model_lineage(),
        feature_lineage=_feature_lineage(),
        competitor_rate_issues=None,
    )


def _gate(gates, code):
    matches = [gate for gate in gates if gate.code == code]
    assert len(matches) == 1, f"expected exactly one {code} gate, got {len(matches)}"
    return matches[0]


NEW_GATE_CODES = ("ROE_FLOOR_MET", "OBJECTIVE_POSITIVE", "VOLUME_NONZERO")


def test_economically_valid_scenario_passes_all_new_gates():
    gates = _evaluate(config=_config_with_roe_floor(0.15))

    for code in NEW_GATE_CODES:
        gate = _gate(gates, code)
        assert gate.status is GateStatus.PASS, code
        assert gate.message
        assert isinstance(gate.evidence, dict)
    assert decision_status_from_gates(gates) is DecisionStatus.RECOMMENDED


def test_roe_gate_passes_when_no_floor_is_configured():
    gates = _evaluate(roe_constraint_relaxed=False, selected_return_on_equity=None)

    roe_gate = _gate(gates, "ROE_FLOOR_MET")
    assert roe_gate.status is GateStatus.PASS
    assert roe_gate.evidence["min_return_on_equity"] is None


def test_roe_gate_warns_when_floor_was_relaxed():
    gates = _evaluate(
        config=_config_with_roe_floor(10.0),
        roe_constraint_relaxed=True,
        selected_return_on_equity=0.45,
    )

    roe_gate = _gate(gates, "ROE_FLOOR_MET")
    assert roe_gate.status is GateStatus.WARN
    assert roe_gate.severity == "warning"
    assert roe_gate.evidence == {
        "min_return_on_equity": 10.0,
        "selected_return_on_equity": 0.45,
        "roe_constraint_relaxed": True,
    }
    assert roe_gate.remediation
    assert decision_status_from_gates(gates) is DecisionStatus.REVIEW_REQUIRED


@pytest.mark.parametrize(
    "selected_roe",
    [None, float("nan"), float("inf"), "not-a-number"],
)
def test_roe_gate_never_passes_on_missing_or_non_finite_evidence(selected_roe):
    gates = _evaluate(
        config=_config_with_roe_floor(0.15),
        roe_constraint_relaxed=False,
        selected_return_on_equity=selected_roe,
    )

    roe_gate = _gate(gates, "ROE_FLOOR_MET")
    assert roe_gate.status is GateStatus.WARN
    assert roe_gate.evidence["selected_return_on_equity"] is None
    assert decision_status_from_gates(gates) is not DecisionStatus.RECOMMENDED


def test_roe_gate_warns_when_relaxed_flag_is_missing_and_floor_configured():
    diagnostics = _clean_diagnostics()
    del diagnostics.roe_constraint_relaxed

    gates = evaluate_gates(
        config=_config_with_roe_floor(0.15),
        diagnostics=diagnostics,
        model_lineage=_synthetic_model_lineage(),
        feature_lineage=_feature_lineage(),
        competitor_rate_issues=None,
    )

    assert _gate(gates, "ROE_FLOOR_MET").status is GateStatus.WARN


def test_roe_gate_warns_when_selected_roe_contradicts_unrelaxed_flag():
    gates = _evaluate(
        config=_config_with_roe_floor(0.50),
        roe_constraint_relaxed=False,
        selected_return_on_equity=0.20,
    )

    assert _gate(gates, "ROE_FLOOR_MET").status is GateStatus.WARN


@pytest.mark.parametrize("objective", [0.0, -269_144.0])
def test_objective_positive_gate_warns_for_zero_or_negative_objective(objective):
    gates = _evaluate(best_result=_best_result(total_objective=objective))

    gate = _gate(gates, "OBJECTIVE_POSITIVE")
    assert gate.status is GateStatus.WARN
    assert gate.evidence["objective_value"] == objective
    assert gate.remediation
    assert _gate(gates, "OBJECTIVE_FINITE").status is GateStatus.PASS
    assert decision_status_from_gates(gates) is DecisionStatus.REVIEW_REQUIRED


def test_objective_positive_gate_pass_message_disclaims_real_world_profitability():
    gate = _gate(_evaluate(), "OBJECTIVE_POSITIVE")

    assert gate.status is GateStatus.PASS
    assert "synthetic" in gate.message.lower()


def test_non_finite_objective_is_still_blocked_and_not_counted_positive():
    gates = _evaluate(best_result=_best_result(total_objective=float("inf")))

    assert _gate(gates, "OBJECTIVE_FINITE").status is GateStatus.BLOCK
    assert _gate(gates, "OBJECTIVE_POSITIVE").status is GateStatus.WARN
    assert decision_status_from_gates(gates) is DecisionStatus.BLOCKED


@pytest.mark.parametrize(
    "overrides,degenerate",
    [
        ({"expected_loan_volume": 0.0}, ["loan"]),
        ({"expected_deposit_volume": 0.0}, ["deposit"]),
        ({"expected_loan_volume": float("nan")}, ["loan"]),
        ({"expected_deposit_volume": True}, ["deposit"]),
        ({"expected_loan_volume": -1.0, "expected_deposit_volume": 0.0}, ["loan", "deposit"]),
    ],
)
def test_volume_gate_warns_for_zero_or_degenerate_volume(overrides, degenerate):
    gates = _evaluate(best_result=_best_result(**overrides))

    gate = _gate(gates, "VOLUME_NONZERO")
    assert gate.status is GateStatus.WARN
    assert gate.evidence["degenerate_products"] == degenerate
    assert "expected_loan_volume" in gate.evidence
    assert "expected_deposit_volume" in gate.evidence
    assert gate.remediation
    assert decision_status_from_gates(gates) is DecisionStatus.REVIEW_REQUIRED


def test_volume_gate_warns_when_volume_evidence_is_missing():
    gates = _evaluate(
        best_result=SimpleNamespace(funding_shortfall=0.0, total_objective=1_000_000.0)
    )

    gate = _gate(gates, "VOLUME_NONZERO")
    assert gate.status is GateStatus.WARN
    assert gate.evidence["degenerate_products"] == ["loan", "deposit"]
