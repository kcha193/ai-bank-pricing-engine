"""Canonical feature-set definitions, versioned and fingerprinted.

Centralizes the feature column lists that already exist in
``modelling/datasets.py`` (``LOAN_FEATURE_COLUMNS`` / ``DEPOSIT_FEATURE_COLUMNS``)
behind a single versioned ``FeatureSchema`` per product, so training and
serving code paths reference one definition instead of duplicating column
lists. Bump the version string whenever the column set or semantics change.
"""

from __future__ import annotations

from balance_pricing_engine.features.contracts import FeatureSchema
from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    DEPOSIT_TARGET_COLUMN,
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
)

FEATURE_SET_VERSION = "v1"

LOAN_RESPONSE_FEATURE_SCHEMA = FeatureSchema(
    name="loan_response_features",
    version=FEATURE_SET_VERSION,
    product="loan",
    columns=tuple(LOAN_FEATURE_COLUMNS),
    target_column=LOAN_TARGET_COLUMN,
)

DEPOSIT_RESPONSE_FEATURE_SCHEMA = FeatureSchema(
    name="deposit_response_features",
    version=FEATURE_SET_VERSION,
    product="deposit",
    columns=tuple(DEPOSIT_FEATURE_COLUMNS),
    target_column=DEPOSIT_TARGET_COLUMN,
)


def feature_schema_for_product(product: str) -> FeatureSchema:
    """Return the canonical feature schema for 'loan' or 'deposit'."""
    if product == "loan":
        return LOAN_RESPONSE_FEATURE_SCHEMA
    if product == "deposit":
        return DEPOSIT_RESPONSE_FEATURE_SCHEMA
    raise ValueError(f"Unknown feature product scope: {product!r}. Expected 'loan' or 'deposit'.")
