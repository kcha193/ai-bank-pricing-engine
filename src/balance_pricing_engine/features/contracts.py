"""Feature-store contracts: schema, lineage, and the provider protocol.

Scope note: this is a synthetic-diagnostic feature layer only. It exists to
give the modelling layer (``modelling/datasets.py``, ``modelling/agentic.py``)
a versioned, fingerprinted schema boundary so training and serving features
are demonstrably drawn from the same definition. It does not introduce
personalised pricing and does not wire unapproved ML output into the
optimizer — that governance boundary remains in
``optimization/response_fn.py`` / ``ModelScoringAgent.resolve_response_fn``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import pandas as pd


class FeatureSchemaError(ValueError):
    """Raised when serving or training data does not match a feature schema."""


@dataclass(frozen=True)
class FeatureSchema:
    """Versioned, fingerprinted definition of a named feature set."""

    name: str
    version: str
    product: str  # "loan" | "deposit"
    columns: tuple[str, ...]
    target_column: str

    @property
    def fingerprint(self) -> str:
        """Stable hash of the schema shape (name, version, columns, target)."""
        payload = {
            "name": self.name,
            "version": self.version,
            "product": self.product,
            "columns": list(self.columns),
            "target_column": self.target_column,
        }
        serialized = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def validate(self, frame: pd.DataFrame, *, require_target: bool = False) -> None:
        """Raise FeatureSchemaError if frame does not satisfy this schema."""
        required = set(self.columns)
        if require_target:
            required.add(self.target_column)
        missing = sorted(required - set(frame.columns))
        if missing:
            raise FeatureSchemaError(
                f"Data is incompatible with feature schema '{self.name}' v{self.version} "
                f"(fingerprint={self.fingerprint[:12]}): missing columns {missing}."
            )


@dataclass(frozen=True)
class FeatureLineage:
    """Lineage metadata recorded alongside a pricing decision or model record."""

    feature_set_name: str
    feature_set_version: str
    schema_fingerprint: str
    product: str
    as_of: str
    row_count: int
    source: str  # e.g. "synthetic_local_store"

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_set_name": self.feature_set_name,
            "feature_set_version": self.feature_set_version,
            "schema_fingerprint": self.schema_fingerprint,
            "product": self.product,
            "as_of": self.as_of,
            "row_count": self.row_count,
            "source": self.source,
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FeatureProvider(Protocol):
    """Capabilities the pricing/modelling layers need from a feature store."""

    def schema(self, product: str) -> FeatureSchema: ...

    def training_dataset(
        self,
        product: str,
        *,
        random_seed: int,
        as_of: str | None = None,
    ) -> tuple[pd.DataFrame, FeatureLineage]: ...

    def serving_features(
        self,
        product: str,
        customers: pd.DataFrame,
        *,
        as_of: str | None = None,
    ) -> tuple[pd.DataFrame, FeatureLineage]: ...
