"""Tests for the narrow champion/challenger shadow-evaluation seam."""

import pandas as pd

from balance_pricing_engine.config import load_config
from balance_pricing_engine.governance.shadow import evaluate_shadow_challenger
from balance_pricing_engine.modelling.agentic import (
    ElasticityBaselineModel,
    LogisticResponseModel,
    ModelRegistry,
)
from balance_pricing_engine.modelling.datasets import LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN
from balance_pricing_engine.modelling.persistence import save_model
from balance_pricing_engine.synthetic.customers import generate_loan_customers
from balance_pricing_engine.utils.random import make_rng


def _sample_features():
    config = load_config("configs/base.yaml")
    rng = make_rng(config.random_seed)
    customers = generate_loan_customers(200, rng)
    frame = customers.copy()
    frame["offered_rate"] = 0.08
    frame["competitor_rate"] = config.market.competitor_loan_rate
    frame["rate_gap_to_competitor"] = frame["offered_rate"] - frame["competitor_rate"]
    frame["risk_segment"] = pd.cut(
        frame["risk_score"], bins=[-1, 0.33, 0.66, 2], labels=[0, 1, 2]
    ).astype(int)
    return frame[LOAN_FEATURE_COLUMNS]


def test_shadow_evaluation_returns_none_without_registry():
    result = evaluate_shadow_challenger(None, LOAN_FEATURE_COLUMNS, "loan", _sample_features())

    assert result is None


def test_shadow_evaluation_returns_none_without_a_challenger(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")

    result = evaluate_shadow_challenger(
        registry, LOAN_FEATURE_COLUMNS, "loan", _sample_features()
    )

    assert result is None


def test_shadow_evaluation_records_error_for_missing_challenger_artifact(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "ChallengerModel",
            "version": "v1",
            "champion_status": "challenger",
            "governance_status": "pending_human_approval",
            "artifact_path": str(tmp_path / "missing.joblib"),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )

    result = evaluate_shadow_challenger(
        registry, LOAN_FEATURE_COLUMNS, "loan", _sample_features()
    )

    assert result is not None
    assert result.error is not None
    assert result.challenger_model_name == "ChallengerModel"


def test_shadow_evaluation_never_raises_on_unexpected_failure(tmp_path, monkeypatch):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "ChallengerModel",
            "version": "v1",
            "champion_status": "challenger",
            "governance_status": "pending_human_approval",
            "artifact_path": str(tmp_path / "missing.joblib"),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(registry, "get_champion", _boom)

    result = evaluate_shadow_challenger(
        registry, LOAN_FEATURE_COLUMNS, "loan", _sample_features()
    )

    assert result is not None
    assert "Shadow evaluation failed" in result.error


def test_shadow_evaluation_scores_champion_and_challenger(tmp_path):
    features = _sample_features()
    dataset = features.copy()
    dataset[LOAN_TARGET_COLUMN] = [0, 1] * (len(dataset) // 2)

    champion_path = tmp_path / "champion.joblib"
    save_model(ElasticityBaselineModel(), champion_path)

    challenger = LogisticResponseModel(random_state=42)
    challenger.fit(dataset, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
    challenger_path = tmp_path / "challenger.joblib"
    save_model(challenger.fitted_model, challenger_path)

    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register(
        {
            "model_name": "ChampionModel",
            "version": "v1",
            "champion_status": "champion",
            "governance_status": "approved",
            "artifact_path": str(champion_path),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )
    registry.register(
        {
            "model_name": "ChallengerModel",
            "version": "v1",
            "champion_status": "challenger",
            "governance_status": "pending_human_approval",
            "artifact_path": str(challenger_path),
            "feature_columns": LOAN_FEATURE_COLUMNS,
        }
    )

    result = evaluate_shadow_challenger(registry, LOAN_FEATURE_COLUMNS, "loan", features)

    assert result is not None
    assert result.error is None
    assert result.champion_model_name == "ChampionModel"
    assert result.challenger_model_name == "ChallengerModel"
    assert result.champion_mean_probability is not None
    assert result.challenger_mean_probability is not None
    assert result.probability_delta == (
        result.challenger_mean_probability - result.champion_mean_probability
    )
