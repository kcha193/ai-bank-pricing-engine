"""Local model persistence helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib


def save_model(model: Any, path: str | Path) -> None:
    """Save a fitted model with joblib."""
    model_path = Path(path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, model_path)


def load_model(path: str | Path) -> Any:
    """Load a fitted model saved with joblib."""
    return joblib.load(Path(path))
