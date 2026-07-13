"""Lightweight, versioned feature-store boundary for synthetic diagnostic models."""

from balance_pricing_engine.features.contracts import (
    FeatureLineage,
    FeatureProvider,
    FeatureSchema,
    FeatureSchemaError,
)
from balance_pricing_engine.features.definitions import (
    DEPOSIT_RESPONSE_FEATURE_SCHEMA,
    LOAN_RESPONSE_FEATURE_SCHEMA,
    feature_schema_for_product,
)
from balance_pricing_engine.features.local_store import LocalFeatureStore

__all__ = [
    "FeatureSchema",
    "FeatureLineage",
    "FeatureProvider",
    "FeatureSchemaError",
    "LOAN_RESPONSE_FEATURE_SCHEMA",
    "DEPOSIT_RESPONSE_FEATURE_SCHEMA",
    "feature_schema_for_product",
    "LocalFeatureStore",
]
