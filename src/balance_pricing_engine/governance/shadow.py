"""Narrow champion/challenger shadow-evaluation seam.

Scores the same synthetic diagnostic feature sample with the current approved
champion and the latest compatible, non-champion challenger in the registry,
purely for comparison. This function:

- never changes the rates selected by the optimizer (it runs after pricing,
  against a static feature sample, and is not wired into
  ``optimization/response_fn``);
- never bypasses champion approval (it does not promote or approve anything);
- never raises — any failure (missing artifact, load error, incompatible
  schema) is recorded on the returned result instead of propagating, so an
  optional, unapproved challenger can never destabilise a pricing run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ShadowEvaluationResult:
    product: str
    champion_model_name: str | None
    challenger_model_name: str | None
    champion_mean_probability: float | None
    challenger_mean_probability: float | None
    probability_delta: float | None
    sample_size: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_shadow_challenger(
    registry: Any | None,
    feature_columns: list[str],
    product: str,
    features: pd.DataFrame,
) -> ShadowEvaluationResult | None:
    """Return a shadow comparison, or None when there is no registry/challenger.

    Returns a result with a populated ``error`` field (never raises) if a
    challenger exists but cannot be scored.
    """
    if registry is None:
        return None
    try:
        champion = registry.get_champion(feature_columns=feature_columns)
        challenger = _latest_challenger(registry, feature_columns, champion)
        if challenger is None:
            return None

        challenger_mean, challenger_error = _mean_probability(challenger, features, feature_columns)
        champion_mean = None
        if champion is not None and challenger_error is None:
            champion_mean, _ = _mean_probability(champion, features, feature_columns)

        delta = (
            None
            if challenger_mean is None or champion_mean is None
            else challenger_mean - champion_mean
        )
        return ShadowEvaluationResult(
            product=product,
            champion_model_name=champion.get("model_name") if champion else None,
            challenger_model_name=challenger.get("model_name"),
            champion_mean_probability=champion_mean,
            challenger_mean_probability=challenger_mean,
            probability_delta=delta,
            sample_size=len(features),
            error=challenger_error,
        )
    except Exception as exc:  # noqa: BLE001 - shadow evaluation must never raise
        return ShadowEvaluationResult(
            product=product,
            champion_model_name=None,
            challenger_model_name=None,
            champion_mean_probability=None,
            challenger_mean_probability=None,
            probability_delta=None,
            sample_size=len(features),
            error=f"Shadow evaluation failed: {exc}",
        )


def _latest_challenger(
    registry: Any,
    feature_columns: list[str],
    champion: dict[str, Any] | None,
) -> dict[str, Any] | None:
    records = registry.load().get("models", [])
    champion_key = None if champion is None else (champion.get("model_name"), champion.get("version"))
    for record in reversed(records):
        if record.get("champion_status") == "champion":
            continue
        if record.get("feature_columns") != feature_columns:
            continue
        if (record.get("model_name"), record.get("version")) == champion_key:
            continue
        return record
    return None


def _mean_probability(
    record: dict[str, Any],
    features: pd.DataFrame,
    feature_columns: list[str],
) -> tuple[float | None, str | None]:
    artifact_path = record.get("artifact_path")
    model_name = record.get("model_name", "<unknown>")
    if not artifact_path or not Path(str(artifact_path)).exists():
        return None, f"Model '{model_name}' artifact is missing; shadow evaluation skipped."
    from balance_pricing_engine.modelling.models import predict_response_probability
    from balance_pricing_engine.modelling.persistence import load_model

    model = load_model(str(artifact_path))
    if hasattr(model, "predict_probability"):
        probabilities = model.predict_probability(features, feature_columns)
    else:
        probabilities = predict_response_probability(model, features, feature_columns)
    return float(np.mean(probabilities)), None
