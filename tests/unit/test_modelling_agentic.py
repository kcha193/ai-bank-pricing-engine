import numpy as np

from balance_pricing_engine.modelling.agentic import (
    ElasticityResponseModel,
    LogisticResponseModel,
    ModelRecord,
    ModelRegistry,
    ModelScoringAgent,
    ModelTrainingAgent,
)
from balance_pricing_engine.modelling.datasets import (
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_loan_response_dataset,
)
from balance_pricing_engine.synthetic.customers import generate_loan_customers
from balance_pricing_engine.utils.random import make_rng


def _build_dataset(seed: int = 99):
    rng = make_rng(seed)
    customers = generate_loan_customers(300, rng)
    return build_loan_response_dataset(
        customers,
        offered_rates=np.array([0.06, 0.065, 0.07]),
        competitor_rate=0.066,
        rate_elasticity=-85.0,
        competitor_gap_elasticity=-65.0,
        rng=rng,
        n_samples=900,
    )


def test_training_agent_trains_candidates_and_writes_registry(tmp_path):
    dataset = _build_dataset()
    agent = ModelTrainingAgent(artifacts_dir=tmp_path, random_seed=7)
    candidates = {
        "elasticity_baseline": ElasticityResponseModel(),
        "logistic": LogisticResponseModel(random_state=7),
    }

    result = agent.train(dataset, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN, candidates)

    assert result.recommended_model_name in {"elasticity_baseline", "logistic"}
    assert len(result.records) == 2
    assert (tmp_path / "model_registry.json").exists()


def test_scoring_agent_fallback_when_artifact_missing(tmp_path):
    dataset = _build_dataset()
    scoring = ModelScoringAgent(registry_path=tmp_path / "model_registry.json")
    fallback = ElasticityResponseModel()

    out = scoring.score("missing_model", dataset, LOAN_FEATURE_COLUMNS, fallback)

    assert out["fallback_used"] is True
    assert 0.0 <= out["min_probability"] <= out["max_probability"] <= 1.0


def test_training_is_reproducible_with_fixed_seed(tmp_path):
    dataset = _build_dataset()
    candidates1 = {
        "elasticity_baseline": ElasticityResponseModel(),
        "logistic": LogisticResponseModel(random_state=21),
    }
    candidates2 = {
        "elasticity_baseline": ElasticityResponseModel(),
        "logistic": LogisticResponseModel(random_state=21),
    }

    result1 = ModelTrainingAgent(artifacts_dir=tmp_path / "a", random_seed=21).train(
        dataset, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN, candidates1
    )
    result2 = ModelTrainingAgent(artifacts_dir=tmp_path / "b", random_seed=21).train(
        dataset, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN, candidates2
    )

    metric1 = next(item for item in result1.report["models"] if item["name"] == "logistic")["metrics"]["log_loss"]
    metric2 = next(item for item in result2.report["models"] if item["name"] == "logistic")["metrics"]["log_loss"]
    assert metric1 == metric2


def test_registry_append_and_latest_lookup(tmp_path):
    registry = ModelRegistry(tmp_path / "model_registry.json")
    record = ModelRecord(
        model_name="elasticity_baseline",
        model_type="ElasticityResponseModel",
        version="v1",
        trained_at_utc="2026-04-29T00:00:00+00:00",
        artifact_path="/tmp/fake.joblib",
        metrics={"auc": 0.5, "log_loss": 0.69, "brier_score": 0.25},
        calibration_mae=0.02,
        governance_status="pending_human_approval",
        champion_status="challenger",
        config_hash="abc",
    )
    registry.append(record)
    latest = registry.latest_for_model("elasticity_baseline")
    assert latest is not None
    assert latest["version"] == "v1"


def test_challenger_not_promoted_when_improvement_threshold_not_met(tmp_path):
    dataset = _build_dataset(seed=123)
    agent = ModelTrainingAgent(artifacts_dir=tmp_path, random_seed=123)
    result = agent.train(
        dataset,
        LOAN_FEATURE_COLUMNS,
        LOAN_TARGET_COLUMN,
        {
            "elasticity_baseline": ElasticityResponseModel(),
            "logistic": LogisticResponseModel(random_state=123),
        },
        min_log_loss_improvement=5.0,
    )
    assert result.recommended_model_name == "elasticity_baseline"
