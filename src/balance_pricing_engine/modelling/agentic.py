"""Governed agentic training/scoring workflow primitives.

This module intentionally keeps training/scoring orchestration lightweight,
reproducible, and auditable. It does not auto-deploy models or auto-set prices.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
    """Protocol for scoreable response models."""

    def fit(self, dataset: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        ...

    def predict_probability(self, dataset: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        ...


@dataclass
class ElasticityResponseModel:
    """Deterministic baseline adapter using provided probabilities from the dataset."""

    probability_column: str = "ground_truth_probability"

    def fit(self, dataset: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        _ = (dataset, feature_columns, target_column)

    def predict_probability(self, dataset: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        _ = feature_columns
        if self.probability_column not in dataset.columns:
            raise ValueError(f"Missing probability column: {self.probability_column}")
        return np.asarray(dataset[self.probability_column].to_numpy(), dtype=float)


@dataclass
class LogisticResponseModel:
    """Configurable logistic model wrapper."""

    random_state: int = 42
    model: Any | None = None

    def fit(self, dataset: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        fitted = fit_response_model(
            build_logistic_regression_model(self.random_state),
            dataset,
            feature_columns,
            target_column,
        )
        self.model = fitted

    def predict_probability(self, dataset: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        if self.model is None:
            raise ValueError("LogisticResponseModel has not been fitted.")
        return predict_response_probability(self.model, dataset, feature_columns)


@dataclass(frozen=True)
class ModelRecord:
    model_name: str
    model_type: str
    version: str
    trained_at_utc: str
    artifact_path: str
    metrics: dict[str, float]
    calibration_mae: float
    governance_status: str
    champion_status: str
    config_hash: str


class ModelRegistry:
    """Lightweight local JSON registry for model artifacts."""

    def __init__(self, registry_path: str | Path):
        self.registry_path = Path(registry_path)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[dict[str, Any]]:
        if not self.registry_path.exists():
            return []
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def append(self, record: ModelRecord) -> None:
        payload = self.load()
        payload.append(asdict(record))
        self.registry_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def latest_for_model(self, model_name: str) -> dict[str, Any] | None:
        """Return latest registry record for a model name."""
        records = self.load()
        return next((record for record in reversed(records) if record["model_name"] == model_name), None)


@dataclass(frozen=True)
class TrainingResult:
    report: dict[str, Any]
    records: list[ModelRecord]
    recommended_model_name: str


class ModelTrainingAgent:
    """Train/evaluate candidate models and recommend a governed champion candidate."""

    def __init__(self, artifacts_dir: str | Path = "artifacts", random_seed: int = 42):
        self.artifacts_dir = Path(artifacts_dir)
        self.random_seed = random_seed
        self.models_dir = self.artifacts_dir / "models"
        self.reports_dir = self.artifacts_dir / "reports"
        self.registry = ModelRegistry(self.artifacts_dir / "model_registry.json")
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def train(
        self,
        dataset: pd.DataFrame,
        feature_columns: list[str],
        target_column: str,
        candidate_models: dict[str, ResponseModel],
        min_log_loss_improvement: float = 0.01,
    ) -> TrainingResult:
        self._validate_schema(dataset, feature_columns, target_column)
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

        for name, model in candidate_models.items():
            model.fit(train_df, feature_columns, target_column)
            val_pred = model.predict_probability(valid_df, feature_columns)
            metrics = evaluate_binary_classifier_adapter(valid_df[target_column].to_numpy(), val_pred)
            calibration = evaluate_calibration_adapter(valid_df[target_column].to_numpy(), val_pred)
            calibration_mae = float((calibration["observed_rate"] - calibration["mean_predicted_probability"]).abs().mean())

            if name == "elasticity_baseline":
                baseline_log_loss = metrics["log_loss"]

            model_rows.append({"name": name, "metrics": metrics, "calibration_mae": calibration_mae})

            artifact_path = self.models_dir / f"{name}.joblib"
            save_model(model, artifact_path)
            record = ModelRecord(
                model_name=name,
                model_type=type(model).__name__,
                version=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
                trained_at_utc=datetime.now(timezone.utc).isoformat(),
                artifact_path=str(artifact_path),
                metrics=metrics,
                calibration_mae=calibration_mae,
                governance_status="pending_human_approval",
                champion_status="challenger",
                config_hash=_stable_hash({"features": feature_columns, "target": target_column, "seed": self.random_seed}),
            )
            self.registry.append(record)
            records.append(record)

        recommended = "elasticity_baseline"
        reason = "Baseline retained as governed default champion."
        if baseline_log_loss is not None:
            for row in model_rows:
                if row["name"] == "elasticity_baseline":
                    continue
                if baseline_log_loss - row["metrics"]["log_loss"] >= min_log_loss_improvement and row["calibration_mae"] <= 0.10:
                    recommended = row["name"]
                    reason = "Challenger improved log loss and passed calibration threshold."
                    break

        report = {
            "train_rows": len(train_df),
            "validation_rows": len(valid_df),
            "test_rows": len(test_df),
            "models": model_rows,
            "recommended_model": recommended,
            "recommendation_reason": reason,
            "human_approval_required": True,
        }
        report_path = self.reports_dir / "training_report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return TrainingResult(report=report, records=records, recommended_model_name=recommended)

    @staticmethod
    def _validate_schema(dataset: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        missing = [column for column in [*feature_columns, target_column] if column not in dataset.columns]
        if missing:
            raise ValueError(f"Dataset missing columns: {missing}")


class ModelScoringAgent:
    """Score inputs with registered model or deterministic fallback."""

    def __init__(self, registry_path: str | Path = "artifacts/model_registry.json"):
        self.registry = ModelRegistry(registry_path)

    def score(
        self,
        model_name: str,
        dataset: pd.DataFrame,
        feature_columns: list[str],
        fallback_model: ResponseModel,
    ) -> dict[str, Any]:
        self._validate_scoring_schema(dataset, feature_columns)
        match = self.registry.latest_for_model(model_name)
        fallback_used = False
        if match is None or not Path(match["artifact_path"]).exists():
            model = fallback_model
            fallback_used = True
            model_version = "fallback"
        else:
            model = load_model(match["artifact_path"])
            model_version = match["version"]

        probabilities = model.predict_probability(dataset, feature_columns)
        result = {
            "model_name": model_name,
            "model_version": model_version,
            "rows": len(dataset),
            "mean_probability": float(np.mean(probabilities)),
            "min_probability": float(np.min(probabilities)),
            "max_probability": float(np.max(probabilities)),
            "fallback_used": fallback_used,
            "config_hash": _stable_hash({"model": model_name, "feature_columns": feature_columns}),
        }
        return result

    @staticmethod
    def _validate_scoring_schema(dataset: pd.DataFrame, feature_columns: list[str]) -> None:
        missing = [column for column in feature_columns if column not in dataset.columns]
        if missing:
            raise ValueError(f"Scoring dataset missing columns: {missing}")


def evaluate_binary_classifier_adapter(y_true: np.ndarray, y_prob: np.ndarray) -> dict[str, float]:
    frame = pd.DataFrame({"target": y_true, "pred": y_prob})
    stub = _ArrayBackedModel(y_prob)
    return evaluate_binary_classifier(stub, frame, ["pred"], "target")


def evaluate_calibration_adapter(y_true: np.ndarray, y_prob: np.ndarray) -> pd.DataFrame:
    frame = pd.DataFrame({"target": y_true, "pred": y_prob})
    stub = _ArrayBackedModel(y_prob)
    return evaluate_calibration(stub, frame, ["pred"], "target", n_bins=5)


@dataclass
class _ArrayBackedModel:
    probs: np.ndarray

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        _ = x
        p = np.clip(self.probs, 0.0, 1.0)
        return np.column_stack([1.0 - p, p])


def _stable_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
