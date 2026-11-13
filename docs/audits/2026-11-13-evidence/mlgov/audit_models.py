"""Helper model classes for audit experiments (importable so joblib can unpickle them)."""
from __future__ import annotations

import numpy as np
import pandas as pd


class InvertedLoanModel:
    """Approved-champion stand-in whose loan acceptance INCREASES with offered rate."""

    name = "InvertedLoanModel"

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        rate = data["offered_rate"].to_numpy(dtype=float)
        return 1 / (1 + np.exp(-(rate - 0.05) * 300.0))


class FlatModel:
    """Constant-probability model (price-insensitive)."""

    name = "FlatModel"

    def predict_probability(self, data: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
        return np.full(len(data), 0.5)
