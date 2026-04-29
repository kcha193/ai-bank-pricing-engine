"""Governed, reproducible agentic model training and scoring workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

import json
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split

from balance_pricing_engine.modelling.evaluation import evaluate_binary_classifier
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


class ElasticityBaselineModel:
    """Baseline adapter using known synthetic ground-truth probabilities."""

    name = "ElasticityBaselineModel"

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        return None

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        if "ground_truth_probability" in data.columns:
            return data["ground_truth_probability"].to_numpy(dtype=float)
        if "offered_rate" not in data.columns:
            raise ValueError("Elasticity baseline requires offered_rate when ground truth is unavailable.")
        # Deterministic fallback proxy when ground-truth column is absent.
        slope = 40.0 if "deposit_amount" in data.columns and "loan_amount" not in data.columns else -40.0
        centered = (data["offered_rate"].to_numpy(dtype=float) - data["offered_rate"].mean()) * slope
        return 1 / (1 + np.exp(-np.clip(centered, -40, 40)))


class LogisticResponseModel:
    """Configurable logistic-response model backed by sklearn."""

    name = "LogisticResponseModel"

    def __init__(self, random_state: int = 42):
        self._random_state = random_state
        self._model: Any | None = None

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
        return json.loads(self.registry_path.read_text())

    def register(self, record: dict[str, Any]) -> None:
        payload = self.load()
        payload.setdefault("models", []).append(record)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(json.dumps(payload, indent=2, sort_keys=True))


class ModelTrainingAgent:
    """Governed orchestrator that trains, compares, and recommends response models."""

    def __init__(self, config: TrainingConfig, artifacts_dir: str | Path = "artifacts"):
        self.config = config
        self.artifacts_dir = Path(artifacts_dir)
        self.model_dir = self.artifacts_dir / "models"
        self.report_dir = self.artifacts_dir / "reports"
        self.registry = ModelRegistry(self.artifacts_dir / "model_registry.json")

    def train_and_recommend(self, dataset: pd.DataFrame, run_name: str) -> dict[str, Any]:
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
        report = {
            "run_name": run_name,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
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
        }
        self.report_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.report_dir / f"{run_name}_training_report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True))

        for candidate in report["candidates"]:
            self.registry.register(
                {
                    "model_name": candidate["model_name"],
                    "run_name": run_name,
                    "timestamp_utc": report["timestamp_utc"],
                    "metrics": candidate["metrics"],
                    "calibration_error": candidate["calibration_error"],
                    "governance_score": candidate["governance_score"],
                    "passed_validation": candidate["passed_validation"],
                    "champion_status": self._champion_status(candidate["model_name"], recommendation),
                    "artifact_path": candidate["artifact_path"],
                    "config_hash": report["config_hash"],
                }
            )
        return report


    def _champion_status(self, model_name: str, recommendation: dict[str, Any]) -> str:
        if model_name == "ElasticityBaselineModel":
            return "baseline_champion"
        if recommendation["recommended_model"] == model_name:
            return "recommended_challenger"
        return "challenger_candidate"
    def _validate_schema(self, dataset: pd.DataFrame) -> None:
        required = set(self.config.feature_columns + [self.config.target_column])
        missing = sorted(required - set(dataset.columns))
        if missing:
            raise ValueError(f"Missing required columns: {missing}")

    def _split_dataset(self, dataset: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
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
        probabilities = model.predict_probability(validation, self.config.feature_columns)
        metrics = evaluate_binary_classifier_proxy(
            validation[self.config.target_column].to_numpy(), probabilities
        )
        calibration_error = float(abs(probabilities.mean() - validation[self.config.target_column].mean()))
        governance_score = float(
            1.0
            - min(1.0, calibration_error * 5)
            - (0.0 if model.name == "ElasticityBaselineModel" else 0.1)
        )
        passed = bool(calibration_error <= self.config.max_calibration_error and np.isfinite(metrics["log_loss"]))
        notes = [
            f"log_loss={metrics['log_loss']:.4f}",
            f"calibration_error={calibration_error:.4f}",
            "transparent baseline" if model.name == "ElasticityBaselineModel" else "learned model",
        ]
        return ModelCandidateResult(
            model_name=model.name,
            metrics=metrics,
            calibration_error=calibration_error,
            governance_score=governance_score,
            artifact_path=artifact_path,
            passed_validation=passed,
            recommendation_notes=notes,
        )

    def _recommend(self, baseline: ModelCandidateResult, challenger: ModelCandidateResult) -> dict[str, Any]:
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
        payload = json.dumps(asdict(self.config), sort_keys=True)
        return sha256(payload.encode("utf-8")).hexdigest()


class ModelScoringAgent:
    """Reproducible scoring orchestrator with deterministic fallback."""

    def __init__(self, feature_columns: list[str], fallback_model: ResponseModel | None = None):
        self.feature_columns = feature_columns
        self.fallback_model = fallback_model or ElasticityBaselineModel()

    def score(
        self,
        dataset: pd.DataFrame,
        model_artifact_path: str | Path | None,
        run_name: str,
    ) -> dict[str, Any]:
        missing = sorted(set(self.feature_columns) - set(dataset.columns))
        if missing:
            raise ValueError(f"Missing required scoring columns: {missing}")

        used_fallback = False
        if model_artifact_path is not None and Path(model_artifact_path).exists():
            model = load_model(model_artifact_path)
            probabilities = predict_response_probability(model, dataset, self.feature_columns)
            model_name = "LoadedLogisticModel"
        else:
            used_fallback = True
            probabilities = self.fallback_model.predict_probability(dataset, self.feature_columns)
            model_name = self.fallback_model.name

        expected_volume = float(np.sum(probabilities))
        result = {
            "run_name": run_name,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "model_used": model_name,
            "used_fallback": used_fallback,
            "rows": len(dataset),
            "mean_probability": float(np.mean(probabilities)),
            "expected_volume": expected_volume,
            "probabilities": probabilities.tolist(),
            "explanation": "Decision-support score only; no autonomous price setting.",
        }
        return result


def evaluate_binary_classifier_proxy(y_true: np.ndarray, y_probability: np.ndarray) -> dict[str, float]:
    """Evaluate probabilities without requiring estimator object."""
    frame = pd.DataFrame({"target": y_true, "p": y_probability})
    model = _StaticProbabilityModel(frame["p"].to_numpy())
    return evaluate_binary_classifier(model, frame.rename(columns={"target": "y"}), ["p"], "y")


class _StaticProbabilityModel:
    def __init__(self, probabilities: np.ndarray):
        self._probabilities = np.asarray(probabilities)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        p = self._probabilities[: len(x)]
        return np.column_stack([1 - p, p])
