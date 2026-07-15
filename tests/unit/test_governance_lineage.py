"""Tests for governance model-lineage re-derivation."""

from balance_pricing_engine.modelling.agentic import ModelRegistry
from balance_pricing_engine.modelling.datasets import LOAN_FEATURE_COLUMNS
from balance_pricing_engine.modelling.persistence import save_model
from balance_pricing_engine.modelling.agentic import ElasticityBaselineModel
from balance_pricing_engine.governance.lineage import (
    artifact_exists,
    resolve_model_lineage,
    synthetic_model_lineage,
)


def test_synthetic_model_lineage_marks_used_fallback():
    lineage = synthetic_model_lineage("loan")

    assert lineage.response_source == "deterministic_synthetic"
    assert lineage.used_fallback is True
    assert lineage.model_name is None


def test_resolve_model_lineage_with_no_registry_is_synthetic():
    lineage = resolve_model_lineage(None, LOAN_FEATURE_COLUMNS, "loan")

    assert lineage == synthetic_model_lineage("loan")


def test_resolve_model_lineage_with_no_champion_is_synthetic(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")

    lineage = resolve_model_lineage(registry, LOAN_FEATURE_COLUMNS, "loan")

    assert lineage.response_source == "deterministic_synthetic"
    assert lineage.used_fallback is True


def test_resolve_model_lineage_with_unapproved_champion_is_synthetic(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "LogisticResponseModel",
            "champion_status": "champion",
            "governance_status": "pending_human_approval",
            "artifact_path": str(tmp_path / "model.joblib"),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )

    lineage = resolve_model_lineage(registry, LOAN_FEATURE_COLUMNS, "loan")

    assert lineage.response_source == "deterministic_synthetic"
    assert lineage.used_fallback is True


def test_resolve_model_lineage_with_approved_champion_reports_champion_metadata(tmp_path):
    artifact_path = tmp_path / "model.joblib"
    save_model(ElasticityBaselineModel(), artifact_path)
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "LogisticResponseModel",
            "version": "v1",
            "champion_status": "champion",
            "governance_status": "approved",
            "artifact_path": str(artifact_path),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )

    lineage = resolve_model_lineage(registry, LOAN_FEATURE_COLUMNS, "loan")

    assert lineage.response_source == "approved_ml_champion"
    assert lineage.model_name == "LogisticResponseModel"
    assert lineage.used_fallback is False
    assert lineage.artifact_path == str(artifact_path)


def test_artifact_exists_helper(tmp_path):
    existing = tmp_path / "present.joblib"
    existing.write_bytes(b"x")

    assert artifact_exists(str(existing)) is True
    assert artifact_exists(str(tmp_path / "missing.joblib")) is False
    assert artifact_exists(None) is False
