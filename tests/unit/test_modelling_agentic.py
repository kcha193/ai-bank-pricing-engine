import numpy as np

from balance_pricing_engine.modelling.agentic import (
    ModelRegistry,
    ModelScoringAgent,
    ModelTrainingAgent,
    TrainingConfig,
)
from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_loan_response_dataset,
)
from balance_pricing_engine.synthetic.customers import generate_loan_customers
from balance_pricing_engine.utils.random import make_rng


def _loan_dataset(seed: int = 123):
    rng = make_rng(seed)
    customers = generate_loan_customers(200, rng)
    return build_loan_response_dataset(
        customers,
        offered_rates=np.array([0.055, 0.06, 0.065, 0.07]),
        competitor_rate=0.062,
        rate_elasticity=-85.0,
        competitor_gap_elasticity=-65.0,
        rng=rng,
        n_samples=1200,
    )


def test_training_agent_trains_and_recommends(tmp_path):
    dataset = _loan_dataset()
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )

    report = agent.train_and_recommend(dataset, run_name="loan")

    assert report["required_human_approval"] is True
    assert len(report["candidates"]) == 2
    assert {c["model_name"] for c in report["candidates"]} == {
        "ElasticityBaselineModel",
        "LogisticResponseModel",
    }


def test_scoring_agent_schema_and_fallback(tmp_path):
    dataset = _loan_dataset().head(20)
    scoring_agent = ModelScoringAgent(feature_columns=LOAN_FEATURE_COLUMNS)
    result = scoring_agent.score(dataset, model_artifact_path=tmp_path / "missing.joblib", run_name="s1")
    assert result["used_fallback"] is True
    assert result["model_used"] == "ElasticityBaselineModel"
    assert len(result["probabilities"]) == len(dataset)


def test_registry_round_trip(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({"model_name": "m1", "version": "v1"})
    payload = registry.load()
    assert payload["models"][0]["model_name"] == "m1"


def test_training_reproducible_with_fixed_seed(tmp_path):
    dataset = _loan_dataset(555)
    config = TrainingConfig(feature_columns=LOAN_FEATURE_COLUMNS, target_column=LOAN_TARGET_COLUMN, random_seed=9)
    report_a = ModelTrainingAgent(config=config, artifacts_dir=tmp_path / "a").train_and_recommend(dataset, "run")
    report_b = ModelTrainingAgent(config=config, artifacts_dir=tmp_path / "b").train_and_recommend(dataset, "run")
    metrics_a = {c["model_name"]: c["metrics"] for c in report_a["candidates"]}
    metrics_b = {c["model_name"]: c["metrics"] for c in report_b["candidates"]}
    assert metrics_a == metrics_b


def test_scoring_fallback_uses_positive_slope_for_deposits(tmp_path):
    dataset = _loan_dataset().head(20).rename(columns={"loan_amount": "deposit_amount"})
    dataset = dataset.drop(columns=["risk_score", "expected_loss_rate", "lvr", "risk_segment", "credit_score", "income", "ground_truth_probability"])
    dataset["relationship_years"] = 3
    dataset["digital_engagement"] = 0.5
    dataset["rate_sensitivity"] = 0.3
    dataset["wealth_segment"] = 1

    scoring_agent = ModelScoringAgent(feature_columns=DEPOSIT_FEATURE_COLUMNS)
    result = scoring_agent.score(dataset, model_artifact_path=tmp_path / "missing.joblib", run_name="deposit")

    ordered = dataset.assign(prob=np.array(result["probabilities"]))[["offered_rate", "prob"]].sort_values("offered_rate")
    assert ordered["prob"].iloc[-1] >= ordered["prob"].iloc[0]


def test_registry_labels_failed_challenger_correctly(tmp_path):
    dataset = _loan_dataset(999)
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=4,
            max_calibration_error=0.0,
        ),
        artifacts_dir=tmp_path,
    )

    agent.train_and_recommend(dataset, run_name="loan")
    records = ModelRegistry(tmp_path / "model_registry.json").load()["models"]
    by_name = {record["model_name"]: record for record in records}

    assert by_name["ElasticityBaselineModel"]["champion_status"] == "baseline_champion"
    assert by_name["LogisticResponseModel"]["champion_status"] == "challenger_candidate"
