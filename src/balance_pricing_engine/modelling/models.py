"""Simple sklearn response model factories and prediction helpers."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


def build_logistic_regression_model(random_state: int = 42) -> Pipeline:
    """Build a logistic regression baseline for binary response modelling."""
    return Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            (
                "classifier",
                LogisticRegression(
                    max_iter=1_000,
                    random_state=random_state,
                    solver="liblinear",
                ),
            ),
        ]
    )


def build_hist_gradient_boosting_model(random_state: int = 42) -> Pipeline:
    """Build a small histogram gradient boosting classifier."""
    return Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            (
                "classifier",
                HistGradientBoostingClassifier(
                    learning_rate=0.05,
                    max_iter=100,
                    random_state=random_state,
                ),
            ),
        ]
    )


def fit_response_model(
    model: Any,
    dataset: pd.DataFrame,
    feature_columns: list[str],
    target_column: str,
) -> Any:
    """Fit a binary response model on selected feature and target columns."""
    return model.fit(dataset[feature_columns], dataset[target_column])


def predict_response_probability(
    model: Any,
    dataset: pd.DataFrame,
    feature_columns: list[str],
) -> np.ndarray:
    """Predict positive-class response probabilities."""
    probabilities = model.predict_proba(dataset[feature_columns])
    return np.asarray(probabilities[:, 1], dtype=float)
