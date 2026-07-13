"""Model lineage recording for the governed response-function resolution path.

``resolve_model_lineage`` re-derives, read-only, the same champion-selection
decision that ``ModelScoringAgent.resolve_response_fn`` already made when the
pricing service called ``run_baseline_diagnostics(registry=...)``. It does not
duplicate the ``GovernanceError`` fail-loud path — if an approved champion's
artifact were missing or unloadable, ``resolve_response_fn`` already raised
before diagnostics could complete, so this function only ever runs after that
resolution succeeded (or was never attempted).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelLineage:
    """Lineage record for the response function that drove one product's pricing."""

    product: str  # "loan" | "deposit"
    response_source: str  # "deterministic_synthetic" | "approved_ml_champion"
    model_name: str | None
    model_version: str | None
    governance_status: str | None
    champion_status: str | None
    artifact_path: str | None
    used_fallback: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "response_source": self.response_source,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "governance_status": self.governance_status,
            "champion_status": self.champion_status,
            "artifact_path": self.artifact_path,
            "used_fallback": self.used_fallback,
        }


def synthetic_model_lineage(product: str) -> ModelLineage:
    """Lineage for the deterministic synthetic fallback (no registry supplied)."""
    return ModelLineage(
        product=product,
        response_source="deterministic_synthetic",
        model_name=None,
        model_version=None,
        governance_status=None,
        champion_status=None,
        artifact_path=None,
        used_fallback=True,
    )


def resolve_model_lineage(
    registry: Any | None,
    feature_columns: list[str],
    product: str,
) -> ModelLineage:
    """Return the ModelLineage for a product, given the same registry inputs
    already passed to ``ModelScoringAgent.resolve_response_fn``.
    """
    if registry is None:
        return synthetic_model_lineage(product)

    champion = registry.get_champion(feature_columns=feature_columns)
    if champion is None or champion.get("governance_status") != "approved":
        return synthetic_model_lineage(product)

    artifact_path = champion.get("artifact_path")
    # If the artifact were missing, resolve_response_fn would already have raised
    # GovernanceError before diagnostics completed, so this branch documents a
    # successfully-loaded approved champion.
    return ModelLineage(
        product=product,
        response_source="approved_ml_champion",
        model_name=champion.get("model_name"),
        model_version=champion.get("version"),
        governance_status=champion.get("governance_status"),
        champion_status=champion.get("champion_status"),
        artifact_path=str(artifact_path) if artifact_path else None,
        used_fallback=False,
    )


def artifact_exists(artifact_path: str | None) -> bool:
    return artifact_path is not None and Path(artifact_path).exists()
