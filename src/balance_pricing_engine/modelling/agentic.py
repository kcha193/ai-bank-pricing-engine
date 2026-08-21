"""Governed, reproducible agentic model training and scoring workflows."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import MISSING, asdict, dataclass, fields, replace
from datetime import datetime, timezone
from hashlib import sha256
from inspect import signature
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np
import pandas as pd
from sklearn.inspection import permutation_importance
from sklearn.model_selection import train_test_split

from balance_pricing_engine.modelling.evaluation import (
    build_calibration_table,
    evaluate_probabilities,
)
from balance_pricing_engine.modelling.models import (
    build_hist_gradient_boosting_model,
    build_logistic_regression_model,
    fit_response_model,
    predict_response_probability,
)
from balance_pricing_engine.modelling.persistence import load_model, save_model

__all__ = [
    "ElasticityBaselineModel",
    "ElasticityResponseModel",
    "LogisticResponseModel",
    "HistGBMResponseModel",
    "BootstrapEnsembleResponseModel",
    "LightGBMResponseModel",
    "XGBoostResponseModel",
    "SurvivalResponseModel",
    "ModelTrainingAgent",
    "ModelScoringAgent",
    "ModelRegistry",
    "ModelRecord",
    "TrainingConfig",
    "TrainingResult",
    "write_model_card",
    "generate_model_cards_from_registry",
    "generate_card_for_champion",
]


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
    feature_columns: list[str] | None = None
    target_column: str | None = None
    run_name: str | None = None
    report_path: str | None = None
    candidate_key: str | None = None


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


class HistGBMResponseModel:
    """Governed HistGradientBoosting response model backed by sklearn."""

    name = "HistGBMResponseModel"

    def __init__(self, random_state: int = 42, model: Any | None = None):
        self._random_state = random_state
        self._model = model
        self._perm_importances: dict[str, float] | None = None

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        fit_data, importance_data = self._split_fit_and_importance_holdout(train, target_column)
        self._model = fit_response_model(
            build_hist_gradient_boosting_model(self._random_state),
            fit_data,
            feature_columns,
            target_column,
        )
        self._perm_importances = None
        if importance_data is None:
            return
        result = permutation_importance(
            self._model,
            importance_data[feature_columns],
            importance_data[target_column],
            n_repeats=5,
            random_state=self._random_state,
            n_jobs=1,
        )
        self._perm_importances = dict(zip(feature_columns, result["importances_mean"].tolist()))
        self._model = fit_response_model(
            build_hist_gradient_boosting_model(self._random_state),
            train,
            feature_columns,
            target_column,
        )

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        if self._model is None:
            raise ValueError("Model must be fitted before scoring.")
        return predict_response_probability(self._model, data, feature_columns)

    def feature_importances(self, feature_columns: list[str]) -> dict[str, float] | None:
        """Return permutation-based feature importances computed at fit time."""
        if self._model is None:
            raise ValueError("Model must be fitted before retrieving feature importances.")
        if self._perm_importances is None:
            return None
        return {col: self._perm_importances.get(col, float("nan")) for col in feature_columns}

    @property
    def fitted_model(self) -> Any:
        return self._model

    def _split_fit_and_importance_holdout(
        self,
        train: pd.DataFrame,
        target_column: str,
    ) -> tuple[pd.DataFrame, pd.DataFrame | None]:
        if len(train) < 5:
            return train, None

        target = train[target_column]
        target_counts = target.value_counts()
        stratify = target if target_counts.size > 1 and target_counts.min() >= 2 else None
        try:
            fit_data, importance_data = train_test_split(
                train,
                test_size=0.2,
                random_state=self._random_state,
                stratify=stratify,
            )
        except ValueError:
            return train, None

        if fit_data.empty or importance_data.empty:
            return train, None
        return fit_data, importance_data


class BootstrapEnsembleResponseModel:
    """Bootstrap ensemble exposing predictive quantile uncertainty on response probability.

    Fits `n_estimators` copies of a base estimator on bootstrap-resampled training rows.
    `predict_probability` (the ResponseModel protocol method) returns the ensemble mean, so
    this slots into `train()` candidates unchanged. `predict_probability_quantiles` exposes the
    spread across replicas — a sampling-noise uncertainty band, not a quantile-loss model
    (sklearn's classifiers here have no quantile-loss objective for probabilities).
    """

    name = "BootstrapEnsembleResponseModel"

    def __init__(
        self,
        n_estimators: int = 25,
        random_state: int = 42,
        base_estimator_factory: Callable[[int], Any] | None = None,
    ):
        if n_estimators < 1:
            raise ValueError(f"n_estimators must be >= 1, got {n_estimators}.")
        self._n_estimators = n_estimators
        self._random_state = random_state
        self._base_estimator_factory = base_estimator_factory or build_hist_gradient_boosting_model
        self._models: list[Any] = []

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        rng = np.random.default_rng(self._random_state)
        n = len(train)
        targets = train[target_column].to_numpy()
        max_attempts = 20
        self._models = []
        for i in range(self._n_estimators):
            for attempt in range(max_attempts):
                idx = rng.integers(0, n, size=n)
                # A single-class draw makes fit_response_model() raise for classifiers
                # (e.g. logistic regression) that require both target classes present.
                if len(np.unique(targets[idx])) >= 2:
                    break
            else:
                raise ValueError(
                    f"Could not draw a bootstrap resample containing both target classes "
                    f"for estimator {i} after {max_attempts} attempts; the outcome is too "
                    "rare for bootstrap ensembling."
                )
            resampled = train.iloc[idx]
            estimator = self._base_estimator_factory(self._random_state + i)
            fit_response_model(estimator, resampled, feature_columns, target_column)
            self._models.append(estimator)

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        return self._predict_matrix(data, feature_columns).mean(axis=0)

    def predict_probability_quantiles(
        self,
        data: pd.DataFrame,
        feature_columns: list[str],
        quantiles: tuple[float, ...] = (0.1, 0.5, 0.9),
    ) -> pd.DataFrame:
        matrix = self._predict_matrix(data, feature_columns)
        columns = {
            f"q{int(round(q * 100))}": np.quantile(matrix, q, axis=0) for q in quantiles
        }
        return pd.DataFrame(columns, index=data.index)

    def _predict_matrix(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        if not self._models:
            raise ValueError("BootstrapEnsembleResponseModel must be fitted before scoring.")
        return np.vstack(
            [predict_response_probability(m, data, feature_columns) for m in self._models]
        )

    @property
    def fitted_model(self) -> Any:
        return self

    def __getstate__(self) -> dict[str, Any]:
        """Drop the base-estimator factory from persisted/pickled state.

        `fitted_model` returns `self`, so this whole wrapper is what gets `joblib.dump`-ed
        as the scoring artifact. The factory is only needed during `fit()`; if a caller
        passed a lambda/closure, persisting it makes the artifact unpicklable and
        `joblib.dump` raises before the model can be registered. Scoring only needs the
        already-fitted `_models`.
        """
        state = self.__dict__.copy()
        state["_base_estimator_factory"] = None
        return state


class LightGBMResponseModel:
    """Governed LightGBM response model. Requires the [ml] optional extra."""

    name = "LightGBMResponseModel"

    def __init__(self, random_state: int = 42, model: Any | None = None):
        self._random_state = random_state
        self._model = model

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        try:
            import lightgbm as lgb
        except ImportError as exc:
            raise ImportError(
                "LightGBMResponseModel requires lightgbm. "
                "Install it with: pip install 'balance_pricing_engine[ml]'"
            ) from exc
        clf = lgb.LGBMClassifier(
            random_state=self._random_state,
            n_estimators=100,
            verbose=-1,
        )
        clf.fit(train[feature_columns], train[target_column])
        self._model = clf

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        if self._model is None:
            raise ValueError("LightGBMResponseModel must be fitted before scoring.")
        proba = self._model.predict_proba(data[feature_columns])
        return proba[:, 1].astype(float)

    def feature_importances(self, feature_columns: list[str]) -> dict[str, float] | None:
        if self._model is None:
            raise ValueError("LightGBMResponseModel must be fitted before retrieving importances.")
        importances = getattr(self._model, "feature_importances_", None)
        if importances is None:
            return None
        return dict(zip(feature_columns, importances.tolist()))

    @property
    def fitted_model(self) -> Any:
        return self._model


class XGBoostResponseModel:
    """Governed XGBoost response model. Requires the [ml] optional extra."""

    name = "XGBoostResponseModel"

    def __init__(self, random_state: int = 42, model: Any | None = None):
        self._random_state = random_state
        self._model = model

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        try:
            import xgboost as xgb
        except ImportError as exc:
            raise ImportError(
                "XGBoostResponseModel requires xgboost. "
                "Install it with: pip install 'balance_pricing_engine[ml]'"
            ) from exc
        clf = xgb.XGBClassifier(
            random_state=self._random_state,
            n_estimators=100,
            eval_metric="logloss",
            verbosity=0,
        )
        clf.fit(train[feature_columns], train[target_column])
        self._model = clf

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        if self._model is None:
            raise ValueError("XGBoostResponseModel must be fitted before scoring.")
        proba = self._model.predict_proba(data[feature_columns])
        return proba[:, 1].astype(float)

    def feature_importances(self, feature_columns: list[str]) -> dict[str, float] | None:
        if self._model is None:
            raise ValueError("XGBoostResponseModel must be fitted before retrieving importances.")
        importances = getattr(self._model, "feature_importances_", None)
        if importances is None:
            return None
        return dict(zip(feature_columns, importances.tolist()))

    @property
    def fitted_model(self) -> Any:
        return self._model


class SurvivalResponseModel:
    """Governed Weibull AFT survival model for churn/rollover prediction.

    Requires the [ml] optional extra (lifelines). Predicts the probability of
    observing the event/churn label by `hold_period_months`, matching the
    project's binary target convention where 1 means event observed.

    Governance note: decision-support only. Must not replace the synthetic
    response functions in the optimizer until trained on real attrition data
    and approved through the ModelRegistry governance workflow.
    """

    name = "SurvivalResponseModel"

    def __init__(
        self,
        hold_period_months: int = 12,
        duration_col: str = "duration",
        event_col: str = "event_observed",
        model: Any | None = None,
    ):
        self._hold_period_months = hold_period_months
        self._duration_col = duration_col
        self._event_col = event_col
        self._model = model

    def fit(self, train: pd.DataFrame, feature_columns: list[str], target_column: str) -> None:
        try:
            import lifelines
        except ImportError as exc:
            raise ImportError(
                "SurvivalResponseModel requires lifelines. "
                "Install it with: pip install 'balance_pricing_engine[ml]'"
            ) from exc
        fitter = lifelines.WeibullAFTFitter()
        fit_columns = list(dict.fromkeys(feature_columns + [self._duration_col, target_column]))
        fit_frame = train[fit_columns].copy()
        fitter.fit(
            fit_frame,
            duration_col=self._duration_col,
            event_col=target_column,
        )
        self._model = fitter

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        """Return event probability by hold_period_months for each row."""
        if self._model is None:
            raise ValueError("SurvivalResponseModel must be fitted before scoring.")
        survival = self._model.predict_survival_function(
            data[feature_columns], times=[self._hold_period_months]
        )
        event_probability = 1.0 - survival.loc[self._hold_period_months].to_numpy(dtype=float)
        return np.clip(event_probability, 0.0, 1.0)

    def feature_importances(self, feature_columns: list[str]) -> dict[str, float] | None:
        """Return absolute lambda-sub-model coefficients as a feature importance proxy.

        Uses lifelines' params_ Series (keys 'lambda__<feature>'). Returns None when
        not yet fitted or when params_ is unavailable.
        """
        if self._model is None:
            return None
        params = getattr(self._model, "params_", None)
        if params is None:
            return None
        result: dict[str, float] = {}
        for col in feature_columns:
            key = f"lambda__{col}"
            if key in params.index:
                result[col] = float(abs(params[key]))
        return result if result else None

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

    def get_champion(
        self,
        *,
        feature_columns: list[str] | None = None,
        target_column: str | None = None,
        config_hash: str | None = None,
        run_name: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the latest compatible champion record, or None."""
        records = self.load().get("models", [])
        return next(
            (
                r
                for r in reversed(records)
                if r.get("champion_status") == "champion"
                and self._matches_filters(
                    r,
                    feature_columns=feature_columns,
                    target_column=target_column,
                    config_hash=config_hash,
                    run_name=run_name,
                )
            ),
            None,
        )

    def approve_model(
        self,
        model_name: str,
        approver: str,
        *,
        version: str | None = None,
        run_name: str | None = None,
        config_hash: str | None = None,
        feature_columns: list[str] | None = None,
        target_column: str | None = None,
    ) -> None:
        """Set governance_status='approved' on the latest matching model record."""
        payload = self.load()
        record = self._find_latest_record(
            payload,
            model_name=model_name,
            version=version,
            run_name=run_name,
            config_hash=config_hash,
            feature_columns=feature_columns,
            target_column=target_column,
        )
        if record is not None:
            self._approve_record(record, approver)
            self.registry_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
            return
        raise ValueError(f"No registry record found for model_name='{model_name}'")

    def promote_champion(
        self,
        model_name: str,
        approver: str,
        *,
        version: str | None = None,
        run_name: str | None = None,
        config_hash: str | None = None,
        feature_columns: list[str] | None = None,
        target_column: str | None = None,
    ) -> None:
        """Approve one matching model record and promote it within its champion scope."""
        payload = self.load()
        target = self._find_latest_record(
            payload,
            model_name=model_name,
            version=version,
            run_name=run_name,
            config_hash=config_hash,
            feature_columns=feature_columns,
            target_column=target_column,
        )
        if target is None:
            raise ValueError(f"No registry record found for model_name='{model_name}'")

        self._approve_record(target, approver)
        target_scope = self._champion_scope(target)
        for record in payload.get("models", []):
            if (
                record is not target
                and record.get("champion_status") == "champion"
                and self._champion_scope(record) == target_scope
            ):
                record["champion_status"] = "retired_champion"
        target["champion_status"] = "champion"
        self.registry_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )

    @staticmethod
    def _approve_record(record: dict[str, Any], approver: str) -> None:
        record["governance_status"] = "approved"
        record["approved_by"] = approver
        record["approved_at_utc"] = datetime.now(timezone.utc).isoformat()

    @classmethod
    def _find_latest_record(
        cls,
        payload: dict[str, Any],
        *,
        model_name: str,
        version: str | None = None,
        run_name: str | None = None,
        config_hash: str | None = None,
        feature_columns: list[str] | None = None,
        target_column: str | None = None,
    ) -> dict[str, Any] | None:
        return next(
            (
                record
                for record in reversed(payload.get("models", []))
                if record.get("model_name") == model_name
                and cls._matches_filters(
                    record,
                    version=version,
                    run_name=run_name,
                    config_hash=config_hash,
                    feature_columns=feature_columns,
                    target_column=target_column,
                )
            ),
            None,
        )

    @staticmethod
    def _matches_filters(
        record: dict[str, Any],
        *,
        version: str | None = None,
        run_name: str | None = None,
        config_hash: str | None = None,
        feature_columns: list[str] | None = None,
        target_column: str | None = None,
    ) -> bool:
        if version is not None and record.get("version") != version:
            return False
        if run_name is not None and record.get("run_name") != run_name:
            return False
        if config_hash is not None and record.get("config_hash") != config_hash:
            return False
        if feature_columns is not None and record.get("feature_columns") != feature_columns:
            return False
        if target_column is not None and record.get("target_column") != target_column:
            return False
        return True

    @staticmethod
    def _champion_scope(record: dict[str, Any]) -> tuple[str, Any]:
        feature_columns = record.get("feature_columns")
        target_column = record.get("target_column")
        if feature_columns is not None:
            return ("features", tuple(feature_columns), target_column)
        config_hash = record.get("config_hash")
        if config_hash is not None:
            return ("config_hash", config_hash)
        return ("global", None)


def write_model_card(
    record: "ModelRecord",
    output_dir: Path,
    feature_columns: list[str] | None = None,
    target_column: str | None = None,
    feature_importances: dict[str, float] | None = None,
    *,
    filename_stem: str | None = None,
) -> Path:
    """Write a markdown model card for a trained model record."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    card_path = output_dir / f"{filename_stem or record.model_name + '_' + record.version}.md"

    features_line = ", ".join(feature_columns) if feature_columns else "see config_hash"
    target_line = target_column if target_column else "see config_hash"
    artifact_line = record.artifact_path if record.artifact_path else "not persisted"
    metrics = record.metrics
    auc = metrics.get("auc", float("nan"))
    log_loss_val = metrics.get("log_loss", float("nan"))
    brier = metrics.get("brier_score", float("nan"))
    importances_section = ""
    if feature_importances is not None:
        importances_section = (
            "## Feature Importances\n"
            "| Feature | Importance |\n"
            "|---------|------------|\n"
            + "".join(
                f"| {feature} | {importance:.4f} |\n"
                for feature, importance in sorted(
                    feature_importances.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )
            )
            + "\n"
        )

    content = (
        f"# Model Card: {record.model_name}\n\n"
        f"## Identity\n"
        f"- **Model type:** {record.model_type}\n"
        f"- **Version:** {record.version}\n"
        f"- **Trained at (UTC):** {record.trained_at_utc}\n"
        f"- **Config hash:** {record.config_hash}\n\n"
        f"## Features & Target\n"
        f"- **Features:** {features_line}\n"
        f"- **Target:** {target_line}\n\n"
        f"## Performance Metrics\n"
        f"| Metric | Value |\n"
        f"|--------|-------|\n"
        f"| AUC | {auc:.4f} |\n"
        f"| Log loss | {log_loss_val:.4f} |\n"
        f"| Brier score | {brier:.4f} |\n"
        f"| Calibration MAE | {record.calibration_mae:.4f} |\n\n"
        f"{importances_section}"
        f"## Governance\n"
        f"- **Governance status:** {record.governance_status}\n"
        f"- **Champion status:** {record.champion_status}\n"
        f"- **Artifact path:** {artifact_line}\n\n"
        f"## Approval\n"
        f"- **Human approval required:** true\n"
    )
    card_path.write_text(content, encoding="utf-8")
    return card_path


def _safe_model_card_stem(*parts: object) -> str:
    raw = "_".join(str(part) for part in parts if part not in (None, ""))
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_")


def _model_record_from_registry_payload(raw: dict[str, Any]) -> ModelRecord | None:
    required_fields = [
        field.name
        for field in fields(ModelRecord)
        if field.default is MISSING and field.default_factory is MISSING
    ]
    if any(name not in raw for name in required_fields):
        return None
    return ModelRecord(**{field.name: raw[field.name] for field in fields(ModelRecord) if field.name in raw})


def generate_model_cards_from_registry(
    registry: "ModelRegistry",
    output_dir: Path | str,
) -> list[Path]:
    """Write a model card for every record in the registry. Returns list of written paths."""
    output_dir = Path(output_dir)
    records = [
        record
        for raw in registry.load().get("models", [])
        if (record := _model_record_from_registry_payload(raw)) is not None
    ]
    base_stems = [
        _safe_model_card_stem(record.model_name, record.version)
        for record in records
    ]
    duplicate_stems = {stem for stem in base_stems if base_stems.count(stem) > 1}
    paths: list[Path] = []
    for index, record in enumerate(records, start=1):
        importances: dict[str, float] | None = None
        if record.artifact_path:
            try:
                from balance_pricing_engine.modelling.persistence import load_model

                model = load_model(record.artifact_path)
                feature_cols = record.feature_columns or []
                importances = _optional_feature_importances(model, feature_cols)
            except Exception:
                importances = None
        base_stem = _safe_model_card_stem(record.model_name, record.version)
        filename_stem = None
        if base_stem in duplicate_stems:
            filename_stem = _safe_model_card_stem(
                record.model_name,
                record.version,
                record.trained_at_utc,
                f"{index:03d}",
            )
        card_path = write_model_card(
            record=record,
            output_dir=output_dir,
            feature_columns=record.feature_columns,
            target_column=record.target_column,
            feature_importances=importances,
            filename_stem=filename_stem,
        )
        paths.append(card_path)
    return paths


def generate_card_for_champion(
    registry: "ModelRegistry",
    output_dir: Path | str,
    *,
    feature_columns: list[str] | None = None,
    target_column: str | None = None,
    config_hash: str | None = None,
    run_name: str | None = None,
) -> Path | None:
    """Write a model card for the current champion, or return None if none exists."""
    champion = registry.get_champion(
        feature_columns=feature_columns,
        target_column=target_column,
        config_hash=config_hash,
        run_name=run_name,
    )
    if champion is None:
        return None
    record = _model_record_from_registry_payload(champion)
    if record is None:
        return None
    importances: dict[str, float] | None = None
    if record.artifact_path:
        try:
            from balance_pricing_engine.modelling.persistence import load_model

            model = load_model(record.artifact_path)
            feature_cols = record.feature_columns or []
            importances = _optional_feature_importances(model, feature_cols)
        except Exception:
            importances = None
    return write_model_card(
        record=record,
        output_dir=Path(output_dir),
        feature_columns=record.feature_columns,
        target_column=record.target_column,
        feature_importances=importances,
    )


def _optional_feature_importances(
    model: ResponseModel | None,
    feature_columns: list[str],
) -> dict[str, float] | None:
    """Return feature importances from models that support the optional hook."""
    if model is None:
        return None
    hook = getattr(model, "feature_importances", None)
    if not callable(hook):
        return None
    try:
        hook_signature = signature(hook)
    except (TypeError, ValueError):
        return None
    try:
        hook_signature.bind(feature_columns)
        importances = hook(feature_columns)
    except Exception:
        return None
    if not isinstance(importances, dict):
        return None
    try:
        return {str(feature): float(importance) for feature, importance in importances.items()}
    except Exception:
        return None


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

    def train(
        self,
        *,
        dataset: pd.DataFrame,
        candidates: dict[str, ResponseModel],
        registry: ModelRegistry,
        feature_columns: list[str] | None = None,
        target_column: str | None = None,
        run_name: str = "training",
        min_log_loss_improvement: float | None = None,
    ) -> TrainingResult:
        """Train candidate response models and write one governed training result."""
        feature_columns = feature_columns or (
            None if self.config is None else self.config.feature_columns
        )
        target_column = target_column or (None if self.config is None else self.config.target_column)
        if feature_columns is None or target_column is None:
            raise ValueError("feature_columns and target_column are required.")
        min_log_loss_improvement = (
            min_log_loss_improvement
            if min_log_loss_improvement is not None
            else (
                self.config.min_log_loss_improvement
                if self.config is not None
                else 0.01
            )
        )
        self._validate_explicit_schema(dataset, feature_columns, target_column)
        if self.config is not None:
            train_df, valid_df, test_df = self._split_dataset(dataset)
        else:
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
        candidate_results: list[ModelCandidateResult] = []
        baseline_log_loss: float | None = None
        timestamp = datetime.now(timezone.utc).isoformat()
        config_hash = _stable_hash(
            {
                "features": feature_columns,
                "target": target_column,
                "seed": self.random_seed,
            }
        )

        for name, model in candidates.items():
            model.fit(train_df, feature_columns, target_column)
            val_pred = model.predict_probability(valid_df, feature_columns)
            metrics = evaluate_probabilities(valid_df[target_column].to_numpy(), val_pred)
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
            artifact_path = self.model_dir / f"{run_name}_{name}.joblib"
            save_model(_scoring_artifact(model), artifact_path)
            candidate_result = ModelCandidateResult(
                model_name=_model_name(model),
                metrics=metrics,
                calibration_error=calibration_error,
                governance_score=_governance_score(model, calibration_error),
                artifact_path=str(artifact_path),
                passed_validation=(
                    calibration_error
                    <= (
                        self.config.max_calibration_error
                        if self.config is not None
                        else 0.10
                    )
                    and bool(np.isfinite(metrics["log_loss"]))
                ),
                recommendation_notes=[
                    f"log_loss={metrics['log_loss']:.4f}",
                    f"calibration_error={calibration_error:.4f}",
                    (
                        "transparent baseline"
                        if _model_name(model) == "ElasticityBaselineModel"
                        else "learned model"
                    ),
                ],
            )
            candidate_results.append(candidate_result)
            record_model_name = _model_name(model) if self.config is not None else name
            record = ModelRecord(
                model_name=record_model_name,
                model_type=type(model).__name__,
                version=_timestamp_slug(),
                trained_at_utc=timestamp,
                artifact_path=str(artifact_path),
                metrics=metrics,
                calibration_mae=calibration_error,
                governance_status="pending_human_approval",
                champion_status="challenger",
                config_hash=config_hash,
                feature_columns=list(feature_columns),
                target_column=target_column,
                run_name=run_name,
                candidate_key=name,
            )
            registry.append(record)
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
        recommendation = {
            "recommended_model": _recommended_report_name(
                recommended,
                candidates,
                config_mode=self.config is not None,
            ),
            "recommend_as": (
                "baseline_champion"
                if recommended == "elasticity_baseline"
                else "challenger_candidate"
            ),
            "reason": reason,
            "log_loss_improvement_vs_baseline": _recommended_improvement(
                baseline_log_loss,
                model_rows,
                recommended,
            ),
        }
        report = {
            "run_name": run_name,
            "timestamp_utc": timestamp,
            "training_config": None if self.config is None else asdict(self.config),
            "config_hash": config_hash,
            "train_rows": len(train_df),
            "validation_rows": len(valid_df),
            "test_rows": len(test_df),
            "models": model_rows,
            "candidates": [asdict(result) for result in candidate_results],
            "recommended_model": recommended,
            "recommendation_reason": reason,
            "recommendation": recommendation,
            "human_approval_required": True,
            "required_human_approval": True,
            "report_path": str(report_path),
        }
        self.report_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        payload = registry.load()
        current_record_keys = {
            (record.model_name, record.version, record.artifact_path, record.config_hash)
            for record in records
        }
        for record in payload.get("models", []):
            record_key = (
                record.get("model_name"),
                record.get("version"),
                record.get("artifact_path"),
                record.get("config_hash"),
            )
            if record_key not in current_record_keys:
                continue
            record["run_name"] = run_name
            record["report_path"] = str(report_path)
            record["feature_columns"] = list(feature_columns)
            record["target_column"] = target_column
            record["champion_status"] = self._champion_status(
                str(record["model_name"]),
                recommendation,
            )
        registry.registry_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        card_feature_columns = feature_columns if self.config is None else self.config.feature_columns
        card_target_column = target_column if self.config is None else self.config.target_column
        fitted_candidates: dict[str, ResponseModel] = dict(candidates)
        for record in records:
            fitted_model = None
            if record.candidate_key is not None:
                fitted_model = fitted_candidates.get(record.candidate_key)
            if fitted_model is None:
                fitted_model = next(
                    (
                        model
                        for key, model in fitted_candidates.items()
                        if key == record.model_name or _model_name(model) == record.model_name
                    ),
                    None,
                )
            importances = _optional_feature_importances(fitted_model, card_feature_columns)
            write_model_card(
                replace(
                    record,
                    champion_status=self._champion_status(record.model_name, recommendation),
                ),
                self.artifacts_dir / "model_cards",
                card_feature_columns,
                card_target_column,
                feature_importances=importances,
            )
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
        metrics = evaluate_probabilities(
            validation[self.config.target_column].to_numpy(),
            probabilities,
        )
        calibration_error = _calibration_error(
            validation[self.config.target_column].to_numpy(),
            probabilities,
        )
        governance_score = _governance_score(model, calibration_error)
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
        registry: "ModelRegistry | None" = None,
    ):
        self.feature_columns = feature_columns
        self.fallback_model = fallback_model or ElasticityBaselineModel()
        self.registry = registry if registry is not None else ModelRegistry(registry_path)

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

    def resolve_response_fn(
        self,
        feature_columns: list[str],
        fallback: Any,
    ) -> Any:
        """Return MLResponseFnAdapter for the approved champion, or fallback.

        Falls back to the synthetic ``fallback`` when there is no approved champion
        (no champion at all, or one that is not yet approved). However, when an
        *approved* champion is selected but its artifact cannot be loaded, that is an
        inconsistent governed state — the registry promises an approved model that
        cannot actually drive prices — and we raise ``GovernanceError`` rather than
        silently repricing on the synthetic fallback.
        """
        from balance_pricing_engine.optimization.response_fn import (
            GovernanceError,
            MLResponseFnAdapter,
        )

        champion = self.registry.get_champion(feature_columns=feature_columns)
        if champion is None:
            return fallback
        if champion.get("governance_status") != "approved":
            return fallback
        model_name = champion.get("model_name", "<unknown>")
        artifact_path = champion.get("artifact_path")
        if artifact_path is None or not Path(str(artifact_path)).exists():
            raise GovernanceError(
                f"Approved champion '{model_name}' has a missing artifact "
                f"(artifact_path={artifact_path!r}); cannot load the governed model. "
                "Repair the registry/artifact before pricing, or retire the champion."
            )
        try:
            model = load_model(str(artifact_path))
        except Exception as exc:  # noqa: BLE001 - surface any load failure as governance error
            raise GovernanceError(
                f"Approved champion '{model_name}' artifact at {artifact_path!r} is "
                f"present but could not be loaded: {exc}. Repair the artifact before pricing."
            ) from exc
        logging.info(
            "Resolved approved champion response model: model_name=%s artifact_path=%s "
            "governance_status=%s",
            champion.get("model_name"),
            artifact_path,
            champion.get("governance_status"),
        )
        return MLResponseFnAdapter(model=model, feature_columns=feature_columns)


def _calibration_error(y_true: np.ndarray, y_probability: np.ndarray) -> float:
    calibration = build_calibration_table(y_true, y_probability, n_bins=5)
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


def _governance_score(model: ResponseModel, calibration_error: float) -> float:
    return float(
        1.0
        - min(1.0, calibration_error * 5)
        - (0.0 if _model_name(model) == "ElasticityBaselineModel" else 0.1)
    )


def _recommended_report_name(
    recommended: str,
    candidates: dict[str, ResponseModel],
    config_mode: bool,
) -> str:
    if config_mode and recommended in candidates:
        return _model_name(candidates[recommended])
    return recommended


def _recommended_improvement(
    baseline_log_loss: float | None,
    model_rows: list[dict[str, Any]],
    recommended: str,
) -> float | None:
    if baseline_log_loss is None:
        return None
    for row in model_rows:
        if row["name"] == recommended:
            return baseline_log_loss - row["metrics"]["log_loss"]
    return None
