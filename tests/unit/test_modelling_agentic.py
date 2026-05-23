from __future__ import annotations

from dataclasses import replace
import logging

import numpy as np
import pandas as pd
import pytest

from balance_pricing_engine.modelling.agentic import (
    ElasticityBaselineModel,
    ElasticityResponseModel,
    HistGBMResponseModel,
    LogisticResponseModel,
    ModelRecord,
    ModelRegistry,
    ModelScoringAgent,
    ModelTrainingAgent,
    TrainingConfig,
    _optional_feature_importances,
    generate_card_for_champion,
    generate_model_cards_from_registry,
    write_model_card,
)
from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    DEPOSIT_TARGET_COLUMN,
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_deposit_response_dataset,
    build_loan_response_dataset,
)
from balance_pricing_engine.optimization.response_fn import (
    MLResponseFnAdapter,
    SyntheticDepositResponseFn,
    SyntheticLoanResponseFn,
)
from balance_pricing_engine.modelling.persistence import save_model
from balance_pricing_engine.synthetic.customers import generate_deposit_customers, generate_loan_customers
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


def _deposit_dataset(seed: int = 456):
    rng = make_rng(seed)
    customers = generate_deposit_customers(200, rng)
    return build_deposit_response_dataset(
        customers,
        offered_rates=np.array([0.04, 0.045, 0.05, 0.055]),
        competitor_rate=0.048,
        rate_elasticity=80.0,
        competitor_gap_elasticity=60.0,
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


def test_histgbm_response_model_fits_and_predicts(tmp_path):
    dataset = _loan_dataset()
    model = HistGBMResponseModel(random_state=42)
    train = dataset.iloc[:800]
    test_data = dataset.iloc[800:]

    model.fit(train, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
    probs = model.predict_probability(test_data, LOAN_FEATURE_COLUMNS)

    assert probs.shape == (len(test_data),)
    assert np.all(probs >= 0) and np.all(probs <= 1)


def test_histgbm_response_model_raises_before_fit():
    model = HistGBMResponseModel(random_state=42)
    df = pd.DataFrame({"offered_rate": [0.08]})
    with pytest.raises(ValueError, match="fitted"):
        model.predict_probability(df, ["offered_rate"])


def test_histgbm_response_model_name():
    assert HistGBMResponseModel.name == "HistGBMResponseModel"


def test_histgbm_response_model_feature_importances_available_after_fit():
    dataset = _loan_dataset()
    model = HistGBMResponseModel(random_state=42)
    model.fit(dataset.iloc[:800], LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)
    importances = model.feature_importances(LOAN_FEATURE_COLUMNS)
    assert isinstance(importances, dict)
    assert set(importances.keys()) == set(LOAN_FEATURE_COLUMNS)
    assert all(isinstance(v, float) for v in importances.values())
    assert all(not np.isnan(v) for v in importances.values())


def test_histgbm_response_model_feature_importances_returns_none_for_tiny_dataset():
    tiny_dataset = pd.DataFrame(
        {
            "offered_rate": [0.055, 0.06, 0.065],
            "competitor_rate": [0.062, 0.062, 0.062],
            "rate_gap_to_competitor": [-0.007, -0.002, 0.003],
            "loan_amount": [250_000.0, 300_000.0, 350_000.0],
            "income": [90_000.0, 110_000.0, 130_000.0],
            "credit_score": [680.0, 720.0, 760.0],
            "risk_score": [0.35, 0.25, 0.2],
            "expected_loss_rate": [0.01, 0.008, 0.006],
            "lvr": [0.75, 0.7, 0.65],
            "risk_segment": [1, 0, 0],
            LOAN_TARGET_COLUMN: [0, 1, 0],
        }
    )
    model = HistGBMResponseModel(random_state=42)

    model.fit(tiny_dataset, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)

    assert model.feature_importances(LOAN_FEATURE_COLUMNS) is None


def test_histgbm_response_model_feature_importances_use_holdout(monkeypatch):
    dataset = _loan_dataset()
    train = dataset.iloc[:800]
    captured = {}

    def capture_permutation_importance(model, features, target, **kwargs):
        captured["feature_index"] = set(features.index)
        captured["target_index"] = set(target.index)
        return {"importances_mean": np.ones(len(LOAN_FEATURE_COLUMNS))}

    monkeypatch.setattr(
        "balance_pricing_engine.modelling.agentic.permutation_importance",
        capture_permutation_importance,
    )

    model = HistGBMResponseModel(random_state=42)
    model.fit(train, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)

    assert captured["feature_index"] == captured["target_index"]
    assert captured["feature_index"].issubset(set(train.index))
    assert captured["feature_index"] != set(train.index)


def test_histgbm_response_model_refits_final_model_on_full_training_data(monkeypatch):
    dataset = _loan_dataset()
    train = dataset.iloc[:800]
    fit_indices = []

    def capture_fit_response_model(model, data, feature_columns, target_column):
        fit_indices.append(set(data.index))
        return model

    def capture_permutation_importance(model, features, target, **kwargs):
        return {"importances_mean": np.ones(len(LOAN_FEATURE_COLUMNS))}

    monkeypatch.setattr(
        "balance_pricing_engine.modelling.agentic.fit_response_model",
        capture_fit_response_model,
    )
    monkeypatch.setattr(
        "balance_pricing_engine.modelling.agentic.permutation_importance",
        capture_permutation_importance,
    )

    model = HistGBMResponseModel(random_state=42)
    model.fit(train, LOAN_FEATURE_COLUMNS, LOAN_TARGET_COLUMN)

    assert fit_indices[0] != set(train.index)
    assert fit_indices[-1] == set(train.index)


def test_histgbm_response_model_in_training_agent(tmp_path):
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
            "histgbm": HistGBMResponseModel(random_state=7),
        },
        registry=agent.registry,
        run_name="loan_three_candidates",
    )
    model_names = {c["model_name"] for c in result.report["candidates"]}
    assert "HistGBMResponseModel" in model_names
    assert len(result.report["candidates"]) == 3


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


def test_registry_promote_champion_can_target_specific_run(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({
        "model_name": "LogisticResponseModel",
        "version": "loan-v1",
        "run_name": "loan",
        "governance_status": "pending_human_approval",
        "champion_status": "challenger",
    })
    registry.register({
        "model_name": "LogisticResponseModel",
        "version": "deposit-v1",
        "run_name": "deposit",
        "governance_status": "pending_human_approval",
        "champion_status": "challenger",
    })

    registry.promote_champion(
        "LogisticResponseModel",
        approver="analyst@bank.nz",
        run_name="loan",
    )

    records = registry.load()["models"]
    loan_record = next(record for record in records if record["run_name"] == "loan")
    deposit_record = next(record for record in records if record["run_name"] == "deposit")
    assert loan_record["champion_status"] == "champion"
    assert loan_record["governance_status"] == "approved"
    assert deposit_record["champion_status"] == "challenger"


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


def _make_record(model_name: str = "logistic", artifact_path: str | None = None) -> ModelRecord:
    return ModelRecord(
        model_name=model_name,
        model_type="LogisticResponseModel",
        version="v1",
        trained_at_utc="2026-05-23T00:00:00+00:00",
        config_hash="abc123",
        metrics={"auc": 0.71, "log_loss": 0.64, "brier_score": 0.22},
        calibration_mae=0.03,
        artifact_path=artifact_path,
        governance_status="trained",
        champion_status="challenger",
        feature_columns=["offered_rate", "competitor_rate"],
        target_column="accepted",
    )


def test_generate_model_cards_from_registry_writes_one_card_per_record(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry = ModelRegistry(registry_path)
    registry.append(_make_record("logistic"))
    registry.append(_make_record("histgbm"))

    cards = generate_model_cards_from_registry(registry, tmp_path / "cards")

    assert len(cards) == 2
    assert all(p.exists() for p in cards)


def test_generate_model_cards_from_registry_preserves_duplicate_model_versions(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry = ModelRegistry(registry_path)
    first = _make_record("logistic")
    second = replace(
        first,
        trained_at_utc="2026-05-23T01:00:00+00:00",
        metrics={"auc": 0.81, "log_loss": 0.54, "brier_score": 0.18},
    )
    registry.append(first)
    registry.append(second)

    cards = generate_model_cards_from_registry(registry, tmp_path / "cards")

    assert len(cards) == 2
    assert len({path.name for path in cards}) == 2
    assert len(list((tmp_path / "cards").glob("*.md"))) == 2
    assert all(path.exists() for path in cards)


def test_generate_model_cards_from_registry_skips_no_records(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry = ModelRegistry(registry_path)

    cards = generate_model_cards_from_registry(registry, tmp_path / "cards")

    assert cards == []


def test_generate_model_cards_from_registry_card_contains_model_name(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry = ModelRegistry(registry_path)
    registry.append(_make_record("logistic"))

    cards = generate_model_cards_from_registry(registry, tmp_path / "cards")
    content = cards[0].read_text()

    assert "logistic" in content
    assert "LogisticResponseModel" in content


def test_generate_card_for_champion_returns_none_when_no_champion(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry = ModelRegistry(registry_path)
    registry.append(_make_record("logistic"))

    result = generate_card_for_champion(registry, tmp_path / "cards")

    assert result is None


def test_generate_card_for_champion_writes_card_for_champion(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry = ModelRegistry(registry_path)
    record = _make_record("logistic")
    registry.append(record)
    registry.approve_model("logistic", approver="test-user")
    registry.promote_champion("logistic", approver="test-user")

    card_path = generate_card_for_champion(registry, tmp_path / "cards")

    assert card_path is not None
    assert card_path.exists()
    content = card_path.read_text()
    assert "champion" in content.lower()


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


def test_scoring_agent_resolve_response_fn_returns_fallback_when_no_champion(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({"model_name": "LogisticResponseModel", "champion_status": "challenger"})
    agent = ModelScoringAgent(registry_path=tmp_path / "registry.json")
    fallback = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)

    fn = agent.resolve_response_fn(feature_columns=LOAN_FEATURE_COLUMNS, fallback=fallback)
    assert fn is fallback


def test_scoring_agent_resolve_response_fn_returns_fallback_when_artifact_missing(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({
        "model_name": "LogisticResponseModel",
        "champion_status": "champion",
        "governance_status": "approved",
        "artifact_path": str(tmp_path / "missing.joblib"),
    })
    agent = ModelScoringAgent(registry_path=tmp_path / "registry.json")
    fallback = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)

    fn = agent.resolve_response_fn(feature_columns=LOAN_FEATURE_COLUMNS, fallback=fallback)
    assert fn is fallback


def test_scoring_agent_resolve_response_fn_returns_ml_adapter_for_approved_champion(
    tmp_path, caplog
):
    from balance_pricing_engine.modelling.agentic import (
        ElasticityBaselineModel, ModelTrainingAgent, TrainingConfig,
    )

    dataset = _loan_dataset()
    agent_trainer = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )
    registry = agent_trainer.registry
    agent_trainer.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "logistic": LogisticResponseModel(random_state=7),
        },
        registry=registry,
        run_name="loan",
    )
    registry.promote_champion("LogisticResponseModel", approver="analyst@bank.nz")

    scoring_agent = ModelScoringAgent(registry_path=tmp_path / "model_registry.json")
    fallback = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)
    caplog.set_level(logging.INFO)
    fn = scoring_agent.resolve_response_fn(feature_columns=LOAN_FEATURE_COLUMNS, fallback=fallback)

    assert isinstance(fn, MLResponseFnAdapter)
    assert "Resolved approved champion response model" in caplog.text
    assert "LogisticResponseModel" in caplog.text
    assert "governance_status=approved" in caplog.text


def test_scoring_agent_resolve_response_fn_uses_feature_compatible_champion(tmp_path):
    loan_artifact = tmp_path / "loan.joblib"
    deposit_artifact = tmp_path / "deposit.joblib"
    save_model(ElasticityBaselineModel(probability_column="loan_probability"), loan_artifact)
    save_model(ElasticityBaselineModel(probability_column="deposit_probability"), deposit_artifact)

    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({
        "model_name": "LogisticResponseModel",
        "champion_status": "champion",
        "governance_status": "approved",
        "artifact_path": str(loan_artifact),
        "feature_columns": LOAN_FEATURE_COLUMNS,
    })
    registry.register({
        "model_name": "LogisticResponseModel",
        "champion_status": "champion",
        "governance_status": "approved",
        "artifact_path": str(deposit_artifact),
        "feature_columns": DEPOSIT_FEATURE_COLUMNS,
    })
    agent = ModelScoringAgent(registry_path=tmp_path / "registry.json")
    fallback = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)

    fn = agent.resolve_response_fn(feature_columns=LOAN_FEATURE_COLUMNS, fallback=fallback)

    assert isinstance(fn, MLResponseFnAdapter)
    assert fn.model.probability_column == "loan_probability"


def test_scoring_agent_resolve_response_fn_falls_back_for_incompatible_champion(tmp_path):
    deposit_artifact = tmp_path / "deposit.joblib"
    save_model(ElasticityBaselineModel(probability_column="deposit_probability"), deposit_artifact)

    registry = ModelRegistry(tmp_path / "registry.json")
    registry.register({
        "model_name": "LogisticResponseModel",
        "champion_status": "champion",
        "governance_status": "approved",
        "artifact_path": str(deposit_artifact),
        "feature_columns": DEPOSIT_FEATURE_COLUMNS,
    })
    agent = ModelScoringAgent(registry_path=tmp_path / "registry.json")
    fallback = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)

    fn = agent.resolve_response_fn(feature_columns=LOAN_FEATURE_COLUMNS, fallback=fallback)

    assert fn is fallback


def test_end_to_end_train_approve_and_use_in_optimizer(tmp_path):
    """
    Full Tier 1 governance flow:
    1. Train logistic challenger on synthetic data.
    2. Approve and promote it to champion in the registry.
    3. Resolve it as a ResponseFn via ModelScoringAgent.
    4. Run evaluate_joint_rate_grid with the resolved fn.
    5. Verify results are valid (same shape as synthetic path).
    """
    from balance_pricing_engine.modelling.agentic import (
        ElasticityBaselineModel, ModelTrainingAgent, TrainingConfig,
    )
    from balance_pricing_engine.optimization.grid_search import evaluate_joint_rate_grid
    from balance_pricing_engine.optimization.response_fn import SyntheticLoanResponseFn
    from balance_pricing_engine.synthetic.customers import (
        generate_deposit_customers, generate_loan_customers,
    )
    from balance_pricing_engine.synthetic.market import MarketState
    from balance_pricing_engine.utils.random import make_rng

    rng = make_rng(42)
    loan_customers = generate_loan_customers(300, rng)
    deposit_customers = generate_deposit_customers(300, rng)
    market = MarketState(
        cash_rate=0.055,
        wholesale_funding_spread=0.012,
        competitor_loan_rate=0.079,
        competitor_deposit_rate=0.048,
    )

    # Step 1: Train
    dataset = _loan_dataset()
    trainer = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=42,
        ),
        artifacts_dir=tmp_path,
    )
    registry = trainer.registry
    trainer.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "logistic": LogisticResponseModel(random_state=42),
        },
        registry=registry,
        run_name="loan_integration",
    )

    # Step 2: Approve and promote
    registry.promote_champion("LogisticResponseModel", approver="integration_test")

    # Step 3: Resolve ResponseFn
    scoring_agent = ModelScoringAgent(registry_path=tmp_path / "model_registry.json")
    fallback = SyntheticLoanResponseFn(elasticity=-85.0, competitor_gap_elasticity=-65.0)
    loan_fn = scoring_agent.resolve_response_fn(
        feature_columns=LOAN_FEATURE_COLUMNS, fallback=fallback
    )
    deposit_response_fn = SyntheticDepositResponseFn(
        elasticity=80.0, competitor_gap_elasticity=60.0
    )

    # Step 4: Run optimizer with ML loan fn
    grid_results = evaluate_joint_rate_grid(
        loan_customers=loan_customers,
        deposit_customers=deposit_customers,
        market=market,
        loan_rate_grid=np.array([0.079, 0.081, 0.083]),
        deposit_rate_grid=np.array([0.047, 0.049]),
        loan_rate_elasticity=-85.0,
        loan_competitor_gap_elasticity=-65.0,
        deposit_rate_elasticity=80.0,
        deposit_competitor_gap_elasticity=60.0,
        required_deposit_funding_ratio=0.8,
        funding_shortfall_penalty_rate=0.5,
        loan_response_fn=loan_fn,
        deposit_response_fn=deposit_response_fn,
    )

    # Step 5: Validate
    assert len(grid_results) == 6  # 3 loan × 2 deposit
    assert grid_results["expected_loan_volume"].gt(0).all()
    assert grid_results["total_objective"].notna().all()


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


def test_training_agent_preserves_historical_record_schema_metadata(tmp_path):
    registry = ModelRegistry(tmp_path / "model_registry.json")
    registry.register({
        "model_name": "LogisticResponseModel",
        "model_type": "LogisticResponseModel",
        "version": "loan-v1",
        "trained_at_utc": "2026-05-20T00:00:00+00:00",
        "artifact_path": "loan.joblib",
        "metrics": {"auc": 0.8, "log_loss": 0.4, "brier_score": 0.16},
        "calibration_mae": 0.03,
        "governance_status": "approved",
        "champion_status": "champion",
        "config_hash": "loan-hash",
        "feature_columns": LOAN_FEATURE_COLUMNS,
        "target_column": LOAN_TARGET_COLUMN,
        "run_name": "loan-history",
    })
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=DEPOSIT_FEATURE_COLUMNS,
            target_column=DEPOSIT_TARGET_COLUMN,
            random_seed=11,
        ),
        artifacts_dir=tmp_path,
    )

    agent.train(
        dataset=_deposit_dataset(),
        candidates={"logistic": LogisticResponseModel(random_state=11)},
        registry=registry,
        run_name="deposit-current",
    )

    historical_record = registry.load()["models"][0]
    assert historical_record["feature_columns"] == LOAN_FEATURE_COLUMNS
    assert historical_record["target_column"] == LOAN_TARGET_COLUMN
    assert historical_record["run_name"] == "loan-history"


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


def test_write_model_card_includes_feature_importances(tmp_path):
    record = ModelRecord(
        model_name="HistGBMResponseModel",
        model_type="HistGBMResponseModel",
        version="20260521000000000000",
        trained_at_utc="2026-05-21T00:00:00+00:00",
        artifact_path=None,
        metrics={"auc": 0.72, "log_loss": 0.64, "brier_score": 0.21},
        calibration_mae=0.04,
        governance_status="pending_human_approval",
        champion_status="challenger",
        config_hash="abc123",
    )
    importances = {"offered_rate": 0.45, "credit_score": 0.30, "loan_amount": 0.25}

    card_path = write_model_card(
        record=record,
        output_dir=tmp_path,
        feature_columns=list(importances.keys()),
        target_column="accepted",
        feature_importances=importances,
    )

    content = card_path.read_text()
    assert "## Feature Importances" in content
    assert "offered_rate" in content
    assert "0.4500" in content


def test_write_model_card_omits_importances_section_when_none(tmp_path):
    record = ModelRecord(
        model_name="LogisticResponseModel",
        model_type="LogisticResponseModel",
        version="20260521000000000001",
        trained_at_utc="2026-05-21T00:00:00+00:00",
        artifact_path=None,
        metrics={"auc": 0.70, "log_loss": 0.66, "brier_score": 0.22},
        calibration_mae=0.05,
        governance_status="pending_human_approval",
        champion_status="challenger",
        config_hash="def456",
    )
    card_path = write_model_card(
        record=record,
        output_dir=tmp_path,
        feature_columns=["offered_rate"],
        target_column="accepted",
    )
    content = card_path.read_text()
    assert "## Feature Importances" not in content


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


def test_training_agent_writes_model_card_with_importances_for_histgbm(tmp_path):
    dataset = _loan_dataset()
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )
    agent.train(
        dataset=dataset,
        candidates={
            "elasticity_baseline": ElasticityBaselineModel(),
            "histgbm": HistGBMResponseModel(random_state=7),
        },
        registry=agent.registry,
        run_name="loan_importances",
    )
    cards_dir = tmp_path / "model_cards"
    card_files = list(cards_dir.glob("HistGBMResponseModel_*.md"))
    assert len(card_files) == 1
    content = card_files[0].read_text()
    assert "## Feature Importances" in content


def test_training_agent_skips_model_card_importances_for_no_arg_hook(tmp_path):
    class NoArgFeatureImportanceModel(LogisticResponseModel):
        name = "NoArgFeatureImportanceModel"

        def feature_importances(self):
            return {"offered_rate": 1.0}

    dataset = _loan_dataset()
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )
    agent.train(
        dataset=dataset,
        candidates={"no_arg": NoArgFeatureImportanceModel(random_state=7)},
        registry=agent.registry,
        run_name="no_arg_importances",
    )

    card_files = list((tmp_path / "model_cards").glob("NoArgFeatureImportanceModel_*.md"))
    assert len(card_files) == 1
    assert "## Feature Importances" not in card_files[0].read_text()


def test_training_agent_skips_model_card_importances_for_non_callable_attr(tmp_path):
    class NonCallableFeatureImportanceModel(LogisticResponseModel):
        name = "NonCallableFeatureImportanceModel"
        feature_importances = {"offered_rate": 1.0}

    dataset = _loan_dataset()
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )
    agent.train(
        dataset=dataset,
        candidates={"non_callable": NonCallableFeatureImportanceModel(random_state=7)},
        registry=agent.registry,
        run_name="non_callable_importances",
    )

    card_files = list((tmp_path / "model_cards").glob("NonCallableFeatureImportanceModel_*.md"))
    assert len(card_files) == 1
    assert "## Feature Importances" not in card_files[0].read_text()


def test_training_agent_skips_model_card_importances_when_hook_raises(tmp_path):
    class RaisingFeatureImportanceModel(LogisticResponseModel):
        name = "RaisingFeatureImportanceModel"

        def feature_importances(self, feature_columns):
            raise NotImplementedError("importances unavailable for this candidate")

    dataset = _loan_dataset()
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )
    agent.train(
        dataset=dataset,
        candidates={"raising": RaisingFeatureImportanceModel(random_state=7)},
        registry=agent.registry,
        run_name="raising_importances",
    )

    card_files = list((tmp_path / "model_cards").glob("RaisingFeatureImportanceModel_*.md"))
    assert len(card_files) == 1
    assert "## Feature Importances" not in card_files[0].read_text()


def test_training_agent_skips_model_card_importances_when_hook_values_are_invalid(tmp_path):
    class InvalidFeatureImportanceModel(LogisticResponseModel):
        name = "InvalidFeatureImportanceModel"

        def feature_importances(self, feature_columns):
            return {feature: "not-a-number" for feature in feature_columns}

    dataset = _loan_dataset()
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )
    agent.train(
        dataset=dataset,
        candidates={"invalid": InvalidFeatureImportanceModel(random_state=7)},
        registry=agent.registry,
        run_name="invalid_importances",
    )

    card_files = list((tmp_path / "model_cards").glob("InvalidFeatureImportanceModel_*.md"))
    assert len(card_files) == 1
    assert "## Feature Importances" not in card_files[0].read_text()


def test_optional_feature_importances_handles_none_return():
    class NoneFeatureImportanceModel:
        def feature_importances(self, feature_columns):
            return None

    assert _optional_feature_importances(NoneFeatureImportanceModel(), ["offered_rate"]) is None


def test_training_agent_uses_candidate_key_for_same_class_model_card_importances(tmp_path):
    class SharedImportanceModel(LogisticResponseModel):
        name = "SharedImportanceModel"

        def __init__(self, offered_rate_importance: float, random_state: int = 7):
            super().__init__(random_state=random_state)
            self._offered_rate_importance = offered_rate_importance

        def feature_importances(self, feature_columns):
            return {
                feature: self._offered_rate_importance if feature == "offered_rate" else 0.0
                for feature in feature_columns
            }

    dataset = _loan_dataset()
    agent = ModelTrainingAgent(
        config=TrainingConfig(
            feature_columns=LOAN_FEATURE_COLUMNS,
            target_column=LOAN_TARGET_COLUMN,
            random_seed=7,
        ),
        artifacts_dir=tmp_path,
    )

    agent.train(
        dataset=dataset,
        candidates={
            "first": SharedImportanceModel(0.9),
            "second": SharedImportanceModel(0.1),
        },
        registry=agent.registry,
        run_name="same_class",
    )

    card_contents = [
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "model_cards").glob("SharedImportanceModel_*.md")
    ]
    assert len(card_contents) == 2
    first_card = next(content for content in card_contents if "same_class_first.joblib" in content)
    second_card = next(content for content in card_contents if "same_class_second.joblib" in content)

    assert "| offered_rate | 0.9000 |" in first_card
    assert "| offered_rate | 0.1000 |" in second_card
