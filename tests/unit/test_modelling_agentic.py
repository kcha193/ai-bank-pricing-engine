import numpy as np
import pytest

from balance_pricing_engine.modelling.agentic import (
    ElasticityBaselineModel,
    ElasticityResponseModel,
    LogisticResponseModel,
    ModelRecord,
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

    result = agent.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "logistic": LogisticResponseModel(random_state=7),
        },
        registry=agent.registry,
        run_name="loan",
    )

    assert not hasattr(agent, "train_and_recommend")
    assert result.report["required_human_approval"] is True
    assert len(result.report["candidates"]) == 2
    assert {c["model_name"] for c in result.report["candidates"]} == {
        "ElasticityBaselineModel",
        "LogisticResponseModel",
    }
    assert result.report_path.endswith(".json")


def test_scoring_agent_schema_and_fallback(tmp_path):
    dataset = _loan_dataset().head(20)
    scoring_agent = ModelScoringAgent(feature_columns=LOAN_FEATURE_COLUMNS)
    result = scoring_agent.score(dataset, model_artifact_path=tmp_path / "missing.joblib", run_name="s1")
    assert result["used_fallback"] is True
    assert result["model_used"] == "ElasticityBaselineModel"
    assert len(result["probabilities"]) == len(dataset)


def test_registry_round_trip_and_latest_lookup(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({"model_name": "m1", "version": "v1"})
    registry.register({"model_name": "m1", "version": "v2"})
    payload = registry.load()
    assert payload["models"][0]["model_name"] == "m1"
    assert registry.latest_for_model("m1")["version"] == "v2"


def test_registry_approve_model_sets_governance_status(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({
        "model_name": "LogisticResponseModel",
        "governance_status": "pending_human_approval",
    })

    registry.approve_model("LogisticResponseModel", approver="analyst@bank.nz")

    record = registry.latest_for_model("LogisticResponseModel")
    assert record["governance_status"] == "approved"
    assert record["approved_by"] == "analyst@bank.nz"
    assert "approved_at_utc" in record


def test_registry_approve_model_raises_when_not_found(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    with pytest.raises(ValueError, match="No registry record found"):
        registry.approve_model("UnknownModel", approver="analyst@bank.nz")


def test_registry_promote_champion_sets_champion_and_retires_previous(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({
        "model_name": "ElasticityBaselineModel",
        "governance_status": "approved",
        "champion_status": "champion",
    })
    registry.register({
        "model_name": "LogisticResponseModel",
        "governance_status": "pending_human_approval",
        "champion_status": "challenger",
    })

    registry.promote_champion("LogisticResponseModel", approver="analyst@bank.nz")

    logistic = registry.latest_for_model("LogisticResponseModel")
    elasticity = registry.latest_for_model("ElasticityBaselineModel")
    assert logistic["champion_status"] == "champion"
    assert logistic["governance_status"] == "approved"
    assert elasticity["champion_status"] == "retired_champion"


def test_registry_get_champion_returns_latest_champion(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({"model_name": "ElasticityBaselineModel", "champion_status": "champion"})
    registry.register({"model_name": "LogisticResponseModel", "champion_status": "challenger"})

    champion = registry.get_champion()
    assert champion["model_name"] == "ElasticityBaselineModel"


def test_registry_get_champion_returns_none_when_no_champion(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({"model_name": "LogisticResponseModel", "champion_status": "challenger"})
    assert registry.get_champion() is None


def test_registry_append_accepts_model_record(tmp_path):
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


def test_registered_scoring_reports_fallback_model_when_artifact_missing(tmp_path):
    dataset = _loan_dataset().head(10)
    registry_path = tmp_path / "model_registry.json"
    registry = ModelRegistry(registry_path)
    registry.register(
        {
            "model_name": "missing_registered_model",
            "version": "v5",
            "artifact_path": str(tmp_path / "does_not_exist.joblib"),
        }
    )
    scoring_agent = ModelScoringAgent(registry_path=registry_path)
    result = scoring_agent._score_registered(
        model_name="missing_registered_model",
        dataset=dataset,
        feature_columns=LOAN_FEATURE_COLUMNS,
        fallback_model=ElasticityResponseModel(),
    )

    assert result["used_fallback"] is True
    assert result["model_used"] == "ElasticityResponseModel"


def test_training_reproducible_with_fixed_seed(tmp_path):
    dataset = _loan_dataset(555)
    config = TrainingConfig(feature_columns=LOAN_FEATURE_COLUMNS, target_column=LOAN_TARGET_COLUMN, random_seed=9)
    agent_a = ModelTrainingAgent(config=config, artifacts_dir=tmp_path / "a")
    agent_b = ModelTrainingAgent(config=config, artifacts_dir=tmp_path / "b")
    result_a = agent_a.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "logistic": LogisticResponseModel(random_state=9),
        },
        registry=agent_a.registry,
        run_name="run",
    )
    result_b = agent_b.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "logistic": LogisticResponseModel(random_state=9),
        },
        registry=agent_b.registry,
        run_name="run",
    )
    metrics_a = {c["model_name"]: c["metrics"] for c in result_a.report["candidates"]}
    metrics_b = {c["model_name"]: c["metrics"] for c in result_b.report["candidates"]}
    assert metrics_a == metrics_b
    assert result_a.report_path != result_b.report_path


def test_lower_level_training_api_returns_versioned_result(tmp_path):
    dataset = _loan_dataset()
    candidates = {
        "elasticity_baseline": ElasticityResponseModel(),
        "logistic": LogisticResponseModel(random_state=21),
    }

    agent = ModelTrainingAgent(artifacts_dir=tmp_path, random_seed=21)
    result = agent.train(
        dataset=dataset,
        feature_columns=LOAN_FEATURE_COLUMNS,
        target_column=LOAN_TARGET_COLUMN,
        candidates=candidates,
        registry=agent.registry,
    )

    assert result.recommended_model_name in {"elasticity_baseline", "logistic"}
    assert len(result.records) == 2
    assert result.report_path.endswith(".json")


def test_challenger_not_promoted_when_improvement_threshold_not_met(tmp_path):
    dataset = _loan_dataset(seed=999)
    agent = ModelTrainingAgent(artifacts_dir=tmp_path, random_seed=123)
    result = agent.train(
        dataset=dataset,
        feature_columns=LOAN_FEATURE_COLUMNS,
        target_column=LOAN_TARGET_COLUMN,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "logistic": LogisticResponseModel(random_state=123),
        },
        registry=agent.registry,
        min_log_loss_improvement=5.0,
    )
    assert result.recommended_model_name == "elasticity_baseline"


def test_lower_level_training_artifacts_are_scoreable(tmp_path):
    dataset = _loan_dataset()
    agent = ModelTrainingAgent(artifacts_dir=tmp_path, random_seed=21)
    result = agent.train(
        dataset=dataset,
        feature_columns=LOAN_FEATURE_COLUMNS,
        target_column=LOAN_TARGET_COLUMN,
        candidates={"logistic": LogisticResponseModel(random_state=21)},
        registry=agent.registry,
    )

    logistic_record = next(record for record in result.records if record.model_name == "logistic")
    scored = ModelScoringAgent(feature_columns=LOAN_FEATURE_COLUMNS).score(
        dataset.head(10),
        model_artifact_path=logistic_record.artifact_path,
        run_name="score-logistic",
    )

    assert scored["used_fallback"] is False
    assert len(scored["probabilities"]) == 10


def test_scoring_fallback_uses_positive_slope_for_deposits(tmp_path):
    dataset = _loan_dataset().head(20).rename(columns={"loan_amount": "deposit_amount"})
    dataset = dataset.drop(
        columns=[
            "risk_score",
            "expected_loss_rate",
            "lvr",
            "risk_segment",
            "credit_score",
            "income",
            "ground_truth_probability",
        ]
    )
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

    agent.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "logistic": LogisticResponseModel(random_state=4),
        },
        registry=agent.registry,
        run_name="loan",
    )
    records = ModelRegistry(tmp_path / "model_registry.json").load()["models"]
    by_name = {record["model_name"]: record for record in records}

    assert by_name["ElasticityBaselineModel"]["champion_status"] == "baseline_champion"
    assert by_name["LogisticResponseModel"]["champion_status"] == "challenger_candidate"


# --- P2-3: write_model_card ---


def _sample_record() -> ModelRecord:
    return ModelRecord(
        model_name="LogisticResponseModel",
        model_type="LogisticResponseModel",
        version="20260520T120000",
        trained_at_utc="2026-05-20T12:00:00+00:00",
        artifact_path="/tmp/model.joblib",
        metrics={"auc": 0.87, "log_loss": 0.41, "brier_score": 0.17},
        calibration_mae=0.04,
        governance_status="pending_human_approval",
        champion_status="challenger",
        config_hash="abc123",
    )


def test_write_model_card_creates_file(tmp_path):
    from balance_pricing_engine.modelling.agentic import write_model_card

    record = _sample_record()
    path = write_model_card(record, tmp_path)
    assert path.exists()
    assert path.stat().st_size > 0
    assert path.name == "LogisticResponseModel_20260520T120000.md"


def test_write_model_card_contains_required_sections(tmp_path):
    from balance_pricing_engine.modelling.agentic import write_model_card

    record = _sample_record()
    path = write_model_card(record, tmp_path)
    content = path.read_text(encoding="utf-8")
    for section in ["## Identity", "## Performance Metrics", "## Governance", "## Approval"]:
        assert section in content, f"Missing section: {section}"


def test_write_model_card_includes_metrics(tmp_path):
    from balance_pricing_engine.modelling.agentic import write_model_card

    record = _sample_record()
    path = write_model_card(record, tmp_path)
    content = path.read_text(encoding="utf-8")
    assert "0.8700" in content  # auc
    assert "0.4100" in content  # log_loss
    assert "0.0400" in content  # calibration_mae


def test_write_model_card_with_feature_columns(tmp_path):
    from balance_pricing_engine.modelling.agentic import write_model_card

    record = _sample_record()
    path = write_model_card(
        record, tmp_path,
        feature_columns=["offered_rate", "credit_score"],
        target_column="accepted",
    )
    content = path.read_text(encoding="utf-8")
    assert "offered_rate, credit_score" in content
    assert "accepted" in content


def test_write_model_card_without_feature_columns_shows_fallback(tmp_path):
    from balance_pricing_engine.modelling.agentic import write_model_card

    record = _sample_record()
    path = write_model_card(record, tmp_path)
    content = path.read_text(encoding="utf-8")
    assert "see config_hash" in content


def test_write_model_card_creates_output_dir(tmp_path):
    from balance_pricing_engine.modelling.agentic import write_model_card

    record = _sample_record()
    nested = tmp_path / "a" / "b" / "model_cards"
    write_model_card(record, nested)
    assert nested.exists()


def test_training_agent_writes_model_cards(tmp_path):
    dataset = _loan_dataset()
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )
    registry = ModelRegistry(tmp_path / "registry.json")
    agent.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "logistic": LogisticResponseModel(random_state=7),
        },
        registry=registry,
        run_name="card_test",
    )
    card_dir = tmp_path / "model_cards"
    assert card_dir.exists()
    cards = list(card_dir.glob("*.md"))
    assert len(cards) == 2  # one per candidate
    content = cards[0].read_text(encoding="utf-8")
    assert "## Identity" in content
    assert "## Approval" in content
