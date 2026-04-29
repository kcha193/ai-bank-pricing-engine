"""Governed, reproducible agentic model training and scoring workflows."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split

from balance_pricing_engine.modelling.evaluation import (
    evaluate_binary_classifier,
    evaluate_calibration,
)
from balance_pricing_engine.modelling.models import (
    build_logistic_regression_model,
    fit_response_model,
    predict_response_probability,
)
from balance_pricing_engine.modelling.persistence import load_model, save_model


class ResponseModel(Protocol):
    """Common interface for portfolio/customer response models."""

    name: str

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        """Fit model on training data."""

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        """Predict response probabilities."""


@dataclass(frozen=True)
class TrainingConfig:
    """Training controls for agentic model comparison."""

    feature_columns: list[str]
    target_column: str
    random_seed: int = 42
    test_size: float = 0.2
    validation_size: float = 0.2
    max_calibration_error: float = 0.08
    min_log_loss_improvement: float = 0.01


@dataclass(frozen=True)
class ModelCandidateResult:
    model_name: str
    metrics: dict[str, float]
    calibration_error: float
    governance_score: float
    artifact_path: str | None
    passed_validation: bool
    recommendation_notes: list[str]


@dataclass(frozen=True)
class ModelRecord:
    """Registry record compatibility shape for branch callers."""

    model_name: str
    model_type: str
    version: str
    trained_at_utc: str
    artifact_path: str | None
    metrics: dict[str, float]
    calibration_mae: float
    governance_status: str
    champion_status: str
    config_hash: str


@dataclass(frozen=True)
class TrainingResult:
    """Compatibility return object for the lower-level candidate training API."""

    report: dict[str, Any]
    report_path: str
    records: list[ModelRecord]
    recommended_model_name: str


class ElasticityBaselineModel:
    """Baseline adapter using known synthetic ground-truth probabilities."""

    name = "ElasticityBaselineModel"

    def __init__(self, probability_column: str = "ground_truth_probability"):
        self.probability_column = probability_column

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        return None

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        if self.probability_column in data.columns:
            return data[self.probability_column].to_numpy(dtype=float)
        if "offered_rate" not in data.columns:
            raise ValueError("Elasticity baseline requires offered_rate when ground truth is unavailable.")

        slope = 40.0 if "deposit_amount" in data.columns and "loan_amount" not in data.columns else -40.0
        centered = (data["offered_rate"].to_numpy(dtype=float) - data["offered_rate"].mean()) * slope
        return 1 / (1 + np.exp(-np.clip(centered, -40, 40)))


class ElasticityResponseModel(ElasticityBaselineModel):
    """Backward-compatible name for the deterministic elasticity baseline."""

    name = "ElasticityResponseModel"


class LogisticResponseModel:
    """Configurable logistic-response model backed by sklearn."""

    name = "LogisticResponseModel"

    def __init__(self, random_state: int = 42, model: Any | None = None):
        self._random_state = random_state
        self._model = model

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        self._model = fit_response_model(
            build_logistic_regression_model(self._random_state),
            train,
            feature_columns,
            target_column,
        )

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        if self._model is None:
            raise ValueError("Model must be fitted before scoring.")
        return predict_response_probability(self._model, data, feature_columns)

    @property
    def fitted_model(self) -> Any:
        return self._model


class ModelRegistry:
    """Lightweight local JSON registry for model lineage and governance."""

    def __init__(self, registry_path: str | Path):
        self.registry_path = Path(registry_path)

    def load(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"models": []}
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def register(self, record: dict[str, Any]) -> None:
        payload = self.load()
        payload.setdefault("models", []).append(record)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def append(self, record: ModelRecord) -> None:
        """Append a dataclass record from the branch-level training API."""
        self.register(asdict(record))

    def latest_for_model(self, model_name: str) -> dict[str, Any] | None:
        """Return latest registry record for a model name."""
        records = self.load().get("models", [])
        return next(
            (record for record in reversed(records) if record.get("model_name") == model_name),
            None,
        )


class ModelTrainingAgent:
    """Governed orchestrator that trains, compares, and recommends response models."""

    def __init__(
        self,
        config: TrainingConfig | None = None,
        artifacts_dir: str | Path = "artifacts",
        random_seed: int = 42,
    ):
        self.config = config
        self.random_seed = config.random_seed if config is not None else random_seed
        self.artifacts_dir = Path(artifacts_dir)
        self.model_dir = self.artifacts_dir / "models"
        self.report_dir = self.artifacts_dir / "reports"
        self.models_dir = self.model_dir
        self.reports_dir = self.report_dir
        self.registry = ModelRegistry(self.artifacts_dir / "model_registry.json")

    def train_and_recommend(self, dataset: pd.DataFrame, run_name: str) -> dict[str, Any]:
        if self.config is None:
            raise ValueError("TrainingConfig is required for train_and_recommend.")

        self._validate_schema(dataset)
        train, validation, test = self._split_dataset(dataset)

        baseline = ElasticityBaselineModel()
        logistic = LogisticResponseModel(self.config.random_seed)
        logistic.fit(train, self.config.feature_columns, self.config.target_column)

        baseline_result = self._evaluate_candidate(
            baseline,
            validation,
            artifact_path=None,
        )
        logistic_artifact = self.model_dir / f"{run_name}_logistic.joblib"
        save_model(logistic.fitted_model, logistic_artifact)
        logistic_result = self._evaluate_candidate(
            logistic,
            validation,
            artifact_path=str(logistic_artifact),
        )

        recommendation = self._recommend(baseline_result, logistic_result)
        timestamp = datetime.now(timezone.utc).isoformat()
        report_path = self.report_dir / f"{run_name}_training_report_{_timestamp_slug()}.json"
        report = {
            "run_name": run_name,
            "timestamp_utc": timestamp,
            "training_config": asdict(self.config),
            "config_hash": self._config_hash(),
            "data": {
                "rows": len(dataset),
                "train_rows": len(train),
                "validation_rows": len(validation),
                "test_rows": len(test),
            },
            "candidates": [asdict(baseline_result), asdict(logistic_result)],
            "recommendation": recommendation,
            "required_human_approval": True,
            "report_path": str(report_path),
        }
        self.report_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

        for candidate in report["candidates"]:
            self.registry.register(
                {
                    "model_name": candidate["model_name"],
                    "run_name": run_name,
                    "version": _timestamp_slug(),
                    "timestamp_utc": timestamp,
                    "metrics": candidate["metrics"],
                    "calibration_error": candidate["calibration_error"],
                    "governance_score": candidate["governance_score"],
                    "passed_validation": candidate["passed_validation"],
                    "governance_status": "pending_human_approval",
                    "champion_status": self._champion_status(
                        candidate["model_name"],
                        recommendation,
                    ),
                    "artifact_path": candidate["artifact_path"],
                    "config_hash": report["config_hash"],
                    "report_path": str(report_path),
                }
            )
        return report

    def train(
        self,
        dataset: pd.DataFrame,
        feature_columns: list[str],
        target_column: str,
        candidate_models: dict[str, ResponseModel],
        min_log_loss_improvement: float = 0.01,
    ) -> TrainingResult:
        """Lower-level candidate training API retained for branch compatibility."""
        self._validate_explicit_schema(dataset, feature_columns, target_column)
        train_df, temp_df = train_test_split(
            dataset,
            test_size=0.4,
            random_state=self.random_seed,
            stratify=dataset[target_column],
        )
        valid_df, test_df = train_test_split(
            temp_df,
            test_size=0.5,
            random_state=self.random_seed,
            stratify=temp_df[target_column],
        )

        records: list[ModelRecord] = []
        model_rows: list[dict[str, Any]] = []
        baseline_log_loss: float | None = None
        timestamp = datetime.now(timezone.utc).isoformat()
        config_hash = _stable_hash(
            {
                "features": feature_columns,
                "target": target_column,
                "seed": self.random_seed,
            }
        )

        for name, model in candidate_models.items():
            model.fit(train_df, feature_columns, target_column)
            val_pred = model.predict_probability(valid_df, feature_columns)
            metrics = evaluate_binary_classifier_proxy(valid_df[target_column].to_numpy(), val_pred)
            calibration_error = _calibration_error(valid_df[target_column].to_numpy(), val_pred)

            if name == "elasticity_baseline":
                baseline_log_loss = metrics["log_loss"]

            model_rows.append(
                {
                    "name": name,
                    "model_name": _model_name(model),
                    "metrics": metrics,
                    "calibration_mae": calibration_error,
                }
            )
            artifact_path = self.model_dir / f"{name}.joblib"
            save_model(_scoring_artifact(model), artifact_path)
            record = ModelRecord(
                model_name=name,
                model_type=type(model).__name__,
                version=_timestamp_slug(),
                trained_at_utc=timestamp,
                artifact_path=str(artifact_path),
                metrics=metrics,
                calibration_mae=calibration_error,
                governance_status="pending_human_approval",
                champion_status="challenger",
                config_hash=config_hash,
            )
            self.registry.append(record)
            records.append(record)

        recommended = "elasticity_baseline"
        reason = "Baseline retained as governed default champion."
        if baseline_log_loss is not None:
            for row in model_rows:
                if row["name"] == "elasticity_baseline":
                    continue
                improvement = baseline_log_loss - row["metrics"]["log_loss"]
                if improvement >= min_log_loss_improvement and row["calibration_mae"] <= 0.10:
                    recommended = row["name"]
                    reason = "Challenger improved log loss and passed calibration threshold."
                    break

        report_path = self.report_dir / f"training_report_{_timestamp_slug()}.json"
        report = {
            "train_rows": len(train_df),
            "validation_rows": len(valid_df),
            "test_rows": len(test_df),
            "models": model_rows,
            "recommended_model": recommended,
            "recommendation_reason": reason,
            "human_approval_required": True,
            "report_path": str(report_path),
        }
        self.report_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        return TrainingResult(
            report=report,
            report_path=str(report_path),
            records=records,
            recommended_model_name=recommended,
        )

    def _champion_status(self, model_name: str, recommendation: dict[str, Any]) -> str:
        if model_name == "ElasticityBaselineModel":
            return "baseline_champion"
        if recommendation["recommended_model"] == model_name:
            return "recommended_challenger"
        return "challenger_candidate"

    def _validate_schema(self, dataset: pd.DataFrame) -> None:
        if self.config is None:
            raise ValueError("TrainingConfig is required for schema validation.")
        self._validate_explicit_schema(
            dataset,
            self.config.feature_columns,
            self.config.target_column,
        )

    @staticmethod
    def _validate_explicit_schema(
        dataset: pd.DataFrame,
        feature_columns: list[str],
        target_column: str,
    ) -> None:
        required = set(feature_columns + [target_column])
        missing = sorted(required - set(dataset.columns))
        if missing:
            raise ValueError(f"Missing required columns: {missing}")

    def _split_dataset(self, dataset: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        if self.config is None:
            raise ValueError("TrainingConfig is required for dataset splitting.")
        train_val, test = train_test_split(
            dataset,
            test_size=self.config.test_size,
            random_state=self.config.random_seed,
            stratify=dataset[self.config.target_column],
        )
        val_frac = self.config.validation_size / (1 - self.config.test_size)
        train, validation = train_test_split(
            train_val,
            test_size=val_frac,
            random_state=self.config.random_seed,
            stratify=train_val[self.config.target_column],
        )
        return train, validation, test

    def _evaluate_candidate(
        self,
        model: ResponseModel,
        validation: pd.DataFrame,
        artifact_path: str | None,
    ) -> ModelCandidateResult:
        if self.config is None:
            raise ValueError("TrainingConfig is required for candidate evaluation.")
        probabilities = model.predict_probability(validation, self.config.feature_columns)
        metrics = evaluate_binary_classifier_proxy(
            validation[self.config.target_column].to_numpy(),
            probabilities,
        )
        calibration_error = _calibration_error(
            validation[self.config.target_column].to_numpy(),
            probabilities,
        )
        governance_score = float(
            1.0
            - min(1.0, calibration_error * 5)
            - (0.0 if _model_name(model) == "ElasticityBaselineModel" else 0.1)
        )
        passed = bool(calibration_error <= self.config.max_calibration_error)
        passed = passed and bool(np.isfinite(metrics["log_loss"]))
        notes = [
            f"log_loss={metrics['log_loss']:.4f}",
            f"calibration_error={calibration_error:.4f}",
            "transparent baseline" if _model_name(model) == "ElasticityBaselineModel" else "learned model",
        ]
        return ModelCandidateResult(
            model_name=_model_name(model),
            metrics=metrics,
            calibration_error=calibration_error,
            governance_score=governance_score,
            artifact_path=artifact_path,
            passed_validation=passed,
            recommendation_notes=notes,
        )

    def _recommend(self, baseline: ModelCandidateResult, challenger: ModelCandidateResult) -> dict[str, Any]:
        if self.config is None:
            raise ValueError("TrainingConfig is required for recommendation.")
        improvement = baseline.metrics["log_loss"] - challenger.metrics["log_loss"]
        if challenger.passed_validation and improvement >= self.config.min_log_loss_improvement:
            return {
                "recommended_model": challenger.model_name,
                "recommend_as": "challenger_candidate",
                "reason": (
                    "Improves validation log loss while passing calibration threshold; "
                    "requires analyst approval before any champion promotion."
                ),
                "log_loss_improvement_vs_baseline": improvement,
            }
        return {
            "recommended_model": baseline.model_name,
            "recommend_as": "baseline_champion",
            "reason": "Challenger did not clear governance/performance gates.",
            "log_loss_improvement_vs_baseline": improvement,
        }

    def _config_hash(self) -> str:
        if self.config is None:
            raise ValueError("TrainingConfig is required for config hash.")
        return _stable_hash(asdict(self.config))


class ModelScoringAgent:
    """Reproducible scoring orchestrator with deterministic fallback."""

    def __init__(
        self,
        feature_columns: list[str] | None = None,
        fallback_model: ResponseModel | None = None,
        registry_path: str | Path = "artifacts/model_registry.json",
    ):
        self.feature_columns = feature_columns
        self.fallback_model = fallback_model or ElasticityBaselineModel()
        self.registry = ModelRegistry(registry_path)

    def score(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if args and isinstance(args[0], str):
            return self._score_registered(*args)
        return self._score_artifact(*args, **kwargs)

    def _score_artifact(
        self,
        dataset: pd.DataFrame,
        model_artifact_path: str | Path | None,
        run_name: str,
    ) -> dict[str, Any]:
        if self.feature_columns is None:
            raise ValueError("feature_columns are required for artifact scoring.")
        self._validate_scoring_schema(dataset, self.feature_columns)

        used_fallback = False
        if model_artifact_path is not None and Path(model_artifact_path).exists():
            model = load_model(model_artifact_path)
            if hasattr(model, "predict_probability"):
                probabilities = model.predict_probability(dataset, self.feature_columns)
            else:
                probabilities = predict_response_probability(model, dataset, self.feature_columns)
            model_name = "LoadedLogisticModel"
        else:
            used_fallback = True
            probabilities = self.fallback_model.predict_probability(dataset, self.feature_columns)
            model_name = _model_name(self.fallback_model)

        return _score_result(
            run_name=run_name,
            model_name=model_name,
            model_version=None,
            rows=len(dataset),
            probabilities=probabilities,
            used_fallback=used_fallback,
            explanation="Decision-support score only; no autonomous price setting.",
        )

    def _score_registered(
        self,
        model_name: str,
        dataset: pd.DataFrame,
        feature_columns: list[str],
        fallback_model: ResponseModel,
    ) -> dict[str, Any]:
        self._validate_scoring_schema(dataset, feature_columns)
        match = self.registry.latest_for_model(model_name)
        used_fallback = False
        scored_model_name = model_name
        if match is None or match.get("artifact_path") is None:
            model = fallback_model
            model_version = "fallback"
            used_fallback = True
            scored_model_name = _model_name(fallback_model)
        elif not Path(str(match["artifact_path"])).exists():
            model = fallback_model
            model_version = "fallback"
            used_fallback = True
            scored_model_name = _model_name(fallback_model)
        else:
            model = load_model(str(match["artifact_path"]))
            model_version = str(match.get("version", "registered"))
            scored_model_name = str(match.get("model_name", model_name))

        if hasattr(model, "predict_probability"):
            probabilities = model.predict_probability(dataset, feature_columns)
        else:
            probabilities = predict_response_probability(model, dataset, feature_columns)

        return _score_result(
            run_name=model_name,
            model_name=scored_model_name,
            model_version=model_version,
            rows=len(dataset),
            probabilities=probabilities,
            used_fallback=used_fallback,
            explanation="Decision-support score only; no autonomous price setting.",
            config_hash=_stable_hash({"model": model_name, "feature_columns": feature_columns}),
        )

    @staticmethod
    def _validate_scoring_schema(dataset: pd.DataFrame, feature_columns: list[str]) -> None:
        missing = sorted(set(feature_columns) - set(dataset.columns))
        if missing:
            raise ValueError(f"Missing required scoring columns: {missing}")


def evaluate_binary_classifier_proxy(y_true: np.ndarray, y_probability: np.ndarray) -> dict[str, float]:
    """Evaluate probabilities without requiring an estimator object."""
    frame = pd.DataFrame({"target": y_true, "p": y_probability})
    model = _StaticProbabilityModel(frame["p"].to_numpy())
    return evaluate_binary_classifier(model, frame.rename(columns={"target": "y"}), ["p"], "y")


def _calibration_error(y_true: np.ndarray, y_probability: np.ndarray) -> float:
    frame = pd.DataFrame({"target": y_true, "p": y_probability})
    model = _StaticProbabilityModel(frame["p"].to_numpy())
    calibration = evaluate_calibration(model, frame, ["p"], "target", n_bins=5)
    delta = calibration["observed_rate"] - calibration["mean_predicted_probability"]
    return float(delta.abs().mean())


def _score_result(
    run_name: str,
    model_name: str,
    model_version: str | None,
    rows: int,
    probabilities: np.ndarray,
    used_fallback: bool,
    explanation: str,
    config_hash: str | None = None,
) -> dict[str, Any]:
    result = {
        "run_name": run_name,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "model_used": model_name,
        "model_name": model_name,
        "model_version": model_version,
        "used_fallback": used_fallback,
        "fallback_used": used_fallback,
        "rows": rows,
        "mean_probability": float(np.mean(probabilities)),
        "min_probability": float(np.min(probabilities)),
        "max_probability": float(np.max(probabilities)),
        "expected_volume": float(np.sum(probabilities)),
        "probabilities": probabilities.tolist(),
        "explanation": explanation,
    }
    if config_hash is not None:
        result["config_hash"] = config_hash
    return result


def _stable_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True)
    return sha256(serialized.encode("utf-8")).hexdigest()


def _scoring_artifact(model: ResponseModel) -> Any:
    """Return an artifact object compatible with scoring-time loading."""
    fitted_model = getattr(model, "fitted_model", None)
    return fitted_model if fitted_model is not None else model


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def _model_name(model: ResponseModel) -> str:
    return getattr(model, "name", type(model).__name__)


class _StaticProbabilityModel:
    def __init__(self, probabilities: np.ndarray):
        self._probabilities = np.asarray(probabilities)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        p = self._probabilities[: len(x)]
        return np.column_stack([1 - p, p])
